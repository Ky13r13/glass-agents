"""Generate and validate an editable, bounded team proposal."""
import contextlib
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

from .agent_config import LIMITS, MAX_MANAGED_AGENTS, covered, schema_dependencies, validate_config, validate_value
from .common import UserError, local_lock, write_json, write_text
from .project import redact
from .runner import codex_command, stream_process


DRAFT_PROVIDERS = {
    "codex": ("Codex", "Check the configured executable and sign-in."),
    "claude-sdk": ("Claude", "Check the Claude Agent SDK and your Claude Code sign-in."),
    "local": ("Ollama", "Check that Ollama is running and the configured model is installed."),
}


def object_shape(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}


def unused_name(base, names):
    index = 1
    candidate = base
    while candidate in names:
        index += 1
        suffix = f"-{index}"
        candidate = base[:40 - len(suffix)] + suffix
    return candidate


def normalize_draft(proposal, config, backends, limit, mode):
    rows = proposal["agents"]
    if not 1 <= len(rows) <= limit:
        raise UserError(f"The planner must return 1 to {limit} agents.")
    agents = {row["name"]: {key: row[key] for key in ("backend", "instructions", "write_scope", "peers")}
              for row in rows}
    if len(rows) != len(agents) or any(agent["backend"] not in backends for agent in agents.values()):
        raise UserError("The planner returned duplicate roles or an unavailable runtime.")
    if mode == "read-only" and any(agent["write_scope"] for agent in agents.values()):
        raise UserError("The planner requested write access for a read-only task.")
    lead, reviewer = proposal["lead"], proposal["reviewer"]
    workflow = ({"type": "feedback", "worker": lead, "reviewer": reviewer} if reviewer
                else {"type": "single", "agent": lead})
    checked = validate_config({"version": 1, "agents": agents, "tools": {}, "workflows": {"draft": workflow}})
    if mode != "read-only" and not agents[lead]["write_scope"]:
        raise UserError("The draft needs a lead with write access to carry out the build.")
    reachable = set()
    frontier = {lead, reviewer} if reviewer else {lead}
    for _ in range(LIMITS["max_depth"]):
        reachable.update(frontier)
        frontier = {peer for name in frontier for peer in agents[name]["peers"]
                    if all(covered(scope, agents[name]["write_scope"]) for scope in agents[peer]["write_scope"])}
        frontier -= reachable
    if reachable != agents.keys():
        raise UserError("Some draft roles cannot be delegated to within the available write scopes or depth. Try drafting again.")
    taken = set(config["agents"])
    names = {}
    for name in agents:
        names[name] = unused_name(name, taken)
        taken.add(names[name])
    roster = []
    for name, agent in checked["agents"].items():
        instructions = re.sub(r"[a-zA-Z][a-zA-Z0-9_-]*", lambda m: names.get(m[0], m[0]), agent["instructions"])
        if mode == "scaffold" and name == lead:
            instructions += ("\n\nFor a blank workspace, create a minimal runnable project in the requested stack, "
                             "including the entry point, dependency manifest where applicable, setup and run instructions, "
                             "and an appropriate smoke check. Inspect the folder before creating files and preserve existing work.")
        if len(instructions) > 12000:
            raise UserError("A draft role's instructions exceed the saved configuration limit.")
        roster.append({"name": names[name], "backend": agent["backend"], "instructions": instructions,
                       "model": None, "write_scope": agent["write_scope"],
                       "peers": [names[peer] for peer in agent["peers"]]})
    summary = proposal["summary"].strip()
    if not summary or len(summary) > 1000:
        raise UserError("The planner must provide a short rationale for the team.")
    return {"agents": roster, "summary": summary, "workflow": {
        "name": unused_name("project-team", config["workflows"]),
        "lead": names[lead], "reviewer": names[reviewer] if reviewer else ""}}


def draft_team(goal, mode, backends, config, cfg, stop, *, provider="codex", progress=None):
    if not isinstance(provider, str) or provider not in DRAFT_PROVIDERS:
        raise UserError("Choose Claude, Codex, or Ollama as the draft provider.")
    label, help_text = DRAFT_PROVIDERS[provider]
    if mode not in ("scaffold", "build", "read-only"):
        raise UserError("Choose whether the team can build files or only produce findings.")
    if not isinstance(backends, list) or not backends or any(not isinstance(b, str) or b not in DRAFT_PROVIDERS for b in backends):
        raise UserError("Select at least one supported team runtime.")
    limit = min(200, MAX_MANAGED_AGENTS - len(config["agents"]))
    if limit < 1:
        raise UserError("The project roster is full. Remove unused agents before drafting another team.")
    schema_dependencies()
    schema = object_shape(
        summary=STRING, lead=STRING, reviewer=STRING,
        agents={"type": "array", "items": object_shape(
            name=STRING, backend={"type": "string", "enum": list(dict.fromkeys(backends))},
            instructions=STRING, write_scope=STRINGS, peers=STRINGS)},
    )
    prompt = (
        "Draft a team configuration for the operator's task below. Return only the structured proposal. "
        "Do not run commands, use tools, inspect files, or start agents during this planning call. "
        f"Choose any useful size from 1 to {limit} based on independent work and coordination cost. "
        "There is no prescribed role structure. Each agent requires a distinct purpose and short actionable "
        "instructions. Name one lead and optionally a distinct read-only reviewer (empty string for none). "
        "Peers are directed delegation and message links. Every role needs a path from the lead or reviewer. "
        f"Agents must be reachable within {LIMITS['max_depth'] - 1} delegation hops. "
        "Tell leads which peers to invoke and when. Only delegate writes within the caller's own allowed scope. "
        "Use project-relative scope paths or globs, with an empty list for read-only. Ollama roles are read-only. "
        "In scaffold and build modes, the lead needs write scope. In scaffold mode, assign the scaffold, "
        "dependency manifest, run instructions and verification to suitable "
        "roles as part of execution. Preserve the requested stack and deployment target. Keep work local unless "
        "the operator authorizes an external action. "
        f"Execution mode: {mode}. In read-only mode all scopes must be empty.\n"
        "Operator task (JSON string):\n" + json.dumps(goal, ensure_ascii=False)
    )
    with TemporaryDirectory(prefix="agentctl-team-draft-") as folder:
        root = Path(folder)
        output = root / "proposal.json"
        env = None
        if provider == "codex":
            contract, schema_file = root / "task.txt", root / "schema.json"
            write_text(contract, prompt)
            write_json(schema_file, schema)
            argv = codex_command(cfg, root, output, "read-only", output_schema=schema_file)
        else:
            contract = root / "request.json"
            write_json(contract, {"provider": provider, "prompt": prompt, "schema": schema, "config": cfg,
                                  "output": str(output), "claude_cli_path": config.get("claude_cli_path")})
            argv = [sys.executable, "-m", "agentctl.draft_worker"]
            env = dict(os.environ, PYTHONUTF8="1")
            package_path = str(Path(__file__).parent.parent)
            env["PYTHONPATH"] = package_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

        def observe(line):
            if progress and line.strip():
                progress("drafting", event=True)

        with local_lock() if provider == "local" else contextlib.nullcontext():
            code = stream_process(argv, root, contract, root, min(300, cfg["timeout_seconds"]), observe, stop=stop,
                                  env=env, on_start=(lambda pid: progress("drafting")) if progress else None)
        if code or not output.is_file():
            with (root / "stderr.log").open("rb") as diagnostics:
                detail = redact(diagnostics.read(2000).decode(errors="replace").strip())
            raise UserError(f"{label} could not draft a team. {help_text}" + (f" {detail}" if detail else ""))
        if output.stat().st_size > 512 * 1024:
            raise UserError("The draft was too large. Ask for shorter role descriptions or a narrower task.")
        if progress:
            progress("validating")
        try:
            proposal = json.loads(output.read_bytes())
            # Check structured output again before interpreting anything provider-produced.
            validate_value(proposal, schema)
            return normalize_draft(proposal, config, backends, limit, mode)
        except (ValueError, KeyError, TypeError) as exc:
            raise UserError(f"{label} returned an invalid team proposal. Try drafting again.") from exc
