"""Project-local definitions for tools, managed agents and feedback workflows."""
from __future__ import annotations

import copy
import fnmatch
import json
from pathlib import Path
import re

from .common import UserError, digest, read_text, state_dir, write_json

NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,39}")
BACKENDS = {"claude-sdk", "codex", "opencode", "local", "codex-local"}
LIMITS = {"max_iterations": 3, "max_agent_calls": 12, "max_tool_calls": 40,
          "max_depth": 3, "timeout_seconds": 900, "max_turns_per_agent": 12,
          "max_reported_cost_usd": 5.0}


def config_path(root, path=None):
    root = Path(root).resolve()
    target = Path(path or ".agentctl/agents.json")
    if not target.is_absolute():
        target = root / target
    if not target.resolve().is_relative_to(root):
        raise UserError("Agent configuration must be inside the project.")
    relative = target.relative_to(root)
    if any(root.joinpath(*relative.parts[:i]).is_symlink() for i in range(1, len(relative.parts) + 1)):
        raise UserError("Agent configuration must not use symlinks.")
    return target


def starter_config():
    return {
        "version": 1,
        "agents": {
            "worker": {"backend": "claude-sdk", "instructions": "Complete the operator's task in the existing project. Reuse existing code. Use project tools and ask advisor for focused help when useful. Respond to review feedback with a focused revision. Preserve unrelated files and project guidance.",
                       "write_scope": ["**"], "peers": ["advisor"], "project_tools": []},
            "reviewer": {"backend": "claude-sdk", "instructions": "Independently inspect the worker's result against the operator's task and actual project evidence. Return accept, revise, or reject with concrete feedback. Do not claim checks ran without tool evidence.",
                         "write_scope": [], "peers": [], "project_tools": []},
            "advisor": {"backend": "claude-sdk", "instructions": "Inspect the requested narrow question and return concise evidence, file paths and uncertainties. Do not edit files.",
                        "write_scope": [], "peers": [], "project_tools": []},
        },
        "tools": {},
        "mcp_servers": {},
        "workflows": {"default": {"worker": "worker", "reviewer": "reviewer",
                                   "required_checks": [], **LIMITS}},
    }


def schema_dependencies():
    try:
        from jsonschema import Draft202012Validator
        return Draft202012Validator
    except ImportError as exc:
        raise UserError("Agent tools need jsonschema. Install the optional runtime: python3 -m pip install '.[agents]' from the agentctl source folder.") from exc


def validate_schema(schema):
    if not isinstance(schema, dict):
        raise UserError("Tool schemas must be JSON Schema objects.")
    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value and (not isinstance(value["$ref"], str) or not value["$ref"].startswith("#")):
                raise UserError("Tool schemas must be self-contained; external $ref values are not supported.")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(schema)
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise UserError(f"Invalid tool JSON Schema: {exc.message}") from exc


def validate_value(value, schema):
    validator = schema_dependencies()(schema)
    error = next(validator.iter_errors(value), None)
    if error:
        location = ".".join(str(p) for p in error.path) or "value"
        raise UserError(f"Invalid {location}: {error.message}")


def valid_name(value):
    return isinstance(value, str) and bool(NAME.fullmatch(value))


def validate_config(supplied):
    if not isinstance(supplied, dict) or supplied.get("version") != 1:
        raise UserError("Agent configuration needs version=1.")
    unknown = set(supplied) - {"version", "agents", "tools", "workflows", "mcp_servers", "claude_cli_path"}
    if unknown:
        raise UserError("Unknown agent configuration fields: " + ", ".join(sorted(unknown)))
    config = copy.deepcopy(supplied)
    agents, tools, workflows = (config.get(k) for k in ("agents", "tools", "workflows"))
    if not isinstance(agents, dict) or not 1 <= len(agents) <= 30:
        raise UserError("Define between 1 and 30 managed agents.")
    if not isinstance(tools, dict) or len(tools) > 40:
        raise UserError("tools must be an object with at most 40 configured commands.")
    if not isinstance(workflows, dict) or not workflows:
        raise UserError("Define at least one feedback workflow.")
    servers = config.setdefault("mcp_servers", {})
    if not isinstance(servers, dict) or not all(valid_name(n) and n != "agentctl" for n in servers):
        raise UserError("MCP servers need valid names; agentctl is reserved.")
    if "claude_cli_path" in config and (not isinstance(config["claude_cli_path"], str) or not config["claude_cli_path"]):
        raise UserError("claude_cli_path must be an executable path.")
    for name, spec in servers.items():
        if not isinstance(spec, dict) or spec.get("type") not in {"stdio", "http", "sse"}:
            raise UserError(f"MCP server {name} needs type stdio, http, or sse.")
        if spec["type"] == "stdio" and not isinstance(spec.get("command"), str):
            raise UserError(f"MCP server {name} needs a command.")
        if spec["type"] != "stdio" and not isinstance(spec.get("url"), str):
            raise UserError(f"MCP server {name} needs a URL.")
    for name, spec in tools.items():
        if not valid_name(name) or not isinstance(spec, dict):
            raise UserError("Each project tool needs a short name and an object definition.")
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(s, str) and s and "\x00" not in s for s in argv):
            raise UserError(f"Tool {name} needs a nonempty argv string array; no shell interpolation is performed.")
        spec.setdefault("description", name)
        spec.setdefault("input_schema", {"type": "object", "additionalProperties": False})
        spec.setdefault("format", "text")
        spec.setdefault("timeout_seconds", 120)
        spec.setdefault("context", {})
        spec.setdefault("cwd", ".")
        if not isinstance(spec["description"], str) or not spec["description"]:
            raise UserError(f"Tool {name} description must be text.")
        if spec["format"] not in {"text", "json"}:
            raise UserError(f"Tool {name} format must be text or json.")
        if type(spec["timeout_seconds"]) is not int or not 1 <= spec["timeout_seconds"] <= 3600:
            raise UserError(f"Tool {name} timeout_seconds must be between 1 and 3600.")
        if not isinstance(spec["cwd"], str) or Path(spec["cwd"]).is_absolute() or ".." in Path(spec["cwd"]).parts:
            raise UserError(f"Tool {name} cwd must stay inside the project.")
        validate_schema(spec["input_schema"])
        if "output_schema" in spec:
            if spec["format"] != "json":
                raise UserError(f"Tool {name} output_schema requires format=json.")
            validate_schema(spec["output_schema"])
    for name, agent in agents.items():
        if not valid_name(name) or name == "controller" or not isinstance(agent, dict):
            raise UserError("Each agent needs a short name and an object definition.")
        agent.setdefault("backend", "claude-sdk")
        agent.setdefault("write_scope", [])
        agent.setdefault("peers", [])
        agent.setdefault("project_tools", [])
        agent.setdefault("mcp_servers", [])
        agent.setdefault("mcp_tools", [])
        if agent["backend"] not in BACKENDS:
            raise UserError(f"Unknown backend for {name}.")
        if not isinstance(agent.get("instructions"), str) or not agent["instructions"].strip() or len(agent["instructions"]) > 12000:
            raise UserError(f"Agent {name} needs instructions of at most 12000 characters.")
        if agent.get("model") is not None and not isinstance(agent["model"], str):
            raise UserError(f"Agent {name} model must be a string.")
        for field in ("write_scope", "peers", "project_tools", "mcp_servers", "mcp_tools"):
            if not isinstance(agent[field], list) or not all(isinstance(s, str) for s in agent[field]):
                raise UserError(f"Agent {name} {field} must be a string array.")
        for scope in agent["write_scope"]:
            if not scope or Path(scope).is_absolute() or ".." in Path(scope).parts:
                raise UserError(f"Agent {name} write scopes must be relative project paths or globs.")
        if any(peer not in agents or peer == name for peer in agent["peers"]):
            raise UserError(f"Agent {name} peers must name other configured agents.")
        if any(tool not in tools for tool in agent["project_tools"]):
            raise UserError(f"Agent {name} references an unknown project tool.")
        if any(server not in servers for server in agent["mcp_servers"]):
            raise UserError(f"Agent {name} references an unknown MCP server.")
        if any(not any(tool.startswith("mcp__" + server + "__") for server in agent["mcp_servers"]) for tool in agent["mcp_tools"]):
            raise UserError(f"Agent {name} mcp_tools must belong to its configured MCP servers.")
        if agent["backend"] in {"local", "codex-local"} and agent["write_scope"]:
            raise UserError("Local peers are read-only.")
        if agent["backend"] == "opencode" and not agent["write_scope"]:
            raise UserError("OpenCode peers use CLI-managed permissions; give them an explicit write scope or use Codex/Claude SDK for readers.")
    for name, workflow in workflows.items():
        if not valid_name(name) or not isinstance(workflow, dict):
            raise UserError("Each workflow needs a short name and an object definition.")
        kind = workflow.setdefault("type", "feedback")
        if kind == "feedback":
            for role in ("worker", "reviewer"):
                if workflow.get(role) not in agents:
                    raise UserError(f"Workflow {name} needs a configured {role}.")
            if workflow["worker"] == workflow["reviewer"]:
                raise UserError("Worker and reviewer must be distinct managed agents.")
            reviewer = agents[workflow["reviewer"]]
            if reviewer["write_scope"]:
                raise UserError("The feedback reviewer must be configured read-only.")
        elif kind == "single":
            if workflow.get("agent") not in agents:
                raise UserError(f"Workflow {name} needs a configured agent.")
        elif kind == "swarm":
            validate_swarm(name, workflow, agents)
        else:
            raise UserError(f"Workflow {name} type must be feedback, single, or swarm.")
        for key, default in LIMITS.items():
            workflow.setdefault(key, default)
            value = workflow[key]
            if key == "max_reported_cost_usd":
                if type(value) not in {int, float} or not 0 < value <= 10000:
                    raise UserError("max_reported_cost_usd must be a positive finite number <= 10000.")
            elif type(value) is not int or not 1 <= value <= (86400 if key == "timeout_seconds" else 1000):
                raise UserError(f"{key} must be a positive bounded integer.")
        workflow.setdefault("required_checks", [])
        if not isinstance(workflow["required_checks"], list) or any(not isinstance(t, str) or t not in tools for t in workflow["required_checks"]):
            raise UserError("required_checks must name configured project tools.")
    return config


SWARM_WRITERS = {"claude-sdk", "codex", "opencode"}
SWARM_LIMITS = {"replicas": (1, 1, 10), "max_rounds": (3, 1, 20), "max_parallel": (4, 1, 16),
                "max_finalists": (3, 1, 16), "min_accepts": (1, 1, 16), "stable_rounds": (1, 1, 20)}
MAX_SWARM_SLOTS = 16


def validate_swarm(name, workflow, agents):
    candidates, critics = workflow.get("candidates"), workflow.get("critics")
    for field, value in (("candidates", candidates), ("critics", critics)):
        if not isinstance(value, list) or not value or len(set(value)) != len(value) or any(a not in agents for a in value):
            raise UserError(f"Swarm {name} {field} must be a nonempty list of distinct configured agents.")
    for agent in candidates:
        if agents[agent]["backend"] not in SWARM_WRITERS or not agents[agent]["write_scope"]:
            raise UserError(f"Swarm candidate {agent} needs a write scope and a claude-sdk, codex or opencode backend.")
    for agent in critics:
        if agents[agent]["write_scope"] or agent in candidates:
            raise UserError(f"Swarm critic {agent} must be a read-only agent that is not also a candidate.")
    for key, (default, low, high) in SWARM_LIMITS.items():
        workflow.setdefault(key, default)
        if type(workflow[key]) is not int or not low <= workflow[key] <= high:
            raise UserError(f"Swarm {name} {key} must be an integer from {low} to {high}.")
    if len(candidates) * workflow["replicas"] > MAX_SWARM_SLOTS:
        raise UserError(f"Swarm {name} has more than {MAX_SWARM_SLOTS} candidate workspaces (candidates × replicas).")
    if workflow["min_accepts"] > len(critics):
        raise UserError(f"Swarm {name} min_accepts cannot exceed the number of critics.")
    if workflow["stable_rounds"] > workflow["max_rounds"]:
        raise UserError(f"Swarm {name} stable_rounds cannot exceed max_rounds.")
    workflow.setdefault("mode", "independent")
    if workflow["mode"] not in {"independent", "adopt_leader"}:
        raise UserError(f"Swarm {name} mode must be independent or adopt_leader.")
    for flag in ("apply_winner", "keep_worktrees"):
        workflow.setdefault(flag, False)
        if not isinstance(workflow[flag], bool):
            raise UserError(f"Swarm {name} {flag} must be true or false.")


def covered(scope, patterns):
    return any(fnmatch.fnmatch(scope, p) or scope.startswith(p.rstrip("/") + "/") for p in patterns)


def ask_config(config, agent, folder=None, scopes=(), read_only=False, checks=(), limits_from="default"):
    """Derive a one-agent workflow; scopes and folders can only narrow configured permissions."""
    config = copy.deepcopy(config)
    if agent not in config["agents"]:
        raise UserError(f"Unknown managed agent: {agent}. Run agents list to see configured names.")
    spec = config["agents"][agent]
    requested = [s.strip() for s in scopes]
    if folder:
        clean = Path(folder).as_posix().strip("/")
        if not clean or clean == "." or Path(clean).is_absolute() or ".." in Path(clean).parts:
            raise UserError("--in must be a relative folder inside the project.")
        requested.append(clean + "/**")
        spec["instructions"] += f"\n\nOperator constraint: work inside the project folder {clean}/ and do not change files elsewhere."
    if read_only:
        narrowed = []
    elif requested:
        if not spec["write_scope"]:
            raise UserError(f"Agent {agent} is read-only in agents.json; a command-line scope cannot grant write access.")
        for scope in requested:
            if not scope or Path(scope).is_absolute() or ".." in Path(scope).parts or not covered(scope, spec["write_scope"]):
                raise UserError(f"Scope {scope} is not inside {agent}'s configured write_scope {spec['write_scope']}; it can only be narrowed.")
        narrowed = requested
    else:
        narrowed = spec["write_scope"]
    if narrowed != spec["write_scope"]:
        spec["write_scope"] = narrowed
        # Delegated writers cannot exceed the narrowed caller.
        for peer in list(spec["peers"]):
            if config["agents"][peer]["write_scope"]:
                if narrowed:
                    config["agents"][peer]["write_scope"] = list(narrowed)
                else:
                    spec["peers"].remove(peer)
    base = config["workflows"].get(limits_from, {})
    workflow = {"type": "single", "agent": agent, "required_checks": list(checks)}
    workflow.update({k: base[k] for k in LIMITS if k in base})
    config["workflows"]["agentctl-ask"] = workflow
    return validate_config(config)


def load_agents(root, path=None):
    target = config_path(root, path)
    if not target.is_file():
        raise UserError("No agent configuration found. Run agentctl agents init in this project.")
    try:
        if target.stat().st_size > 512 * 1024:
            raise UserError("Agent configuration exceeds 512 KiB.")
        return validate_config(json.loads(read_text(target))), target
    except json.JSONDecodeError as exc:
        raise UserError(f"Invalid agent configuration JSON: {exc}") from exc


def initialize_agents(root):
    path = config_path(root)
    if path.exists():
        return {"path": str(path), "created": False, "message": "Existing agent definitions preserved."}
    config = starter_config()
    write_json(path, config)
    ignore = state_dir(root) / ".gitignore"
    if ignore.is_file() and not ignore.is_symlink() and read_text(ignore) == "*\n":
        from .common import write_text
        write_text(ignore, "*\n!agents.json\n")
    return {"path": str(path), "created": True, "agents": list(config["agents"]),
            "next": "Review agent scopes and add your project's commands to tools before running a workflow."}


def run_succeeded(record, interactive_ok=False):
    """Exit-status rule: managed runs need an accepted loop (or a completed single-agent task with passing checks)."""
    statuses = {"completed", "interactive_closed"} if interactive_ok else {"completed"}
    if record["status"] not in statuses:
        return False
    return record["backend"] != "claude-sdk" or record.get("loop_outcome") in {"accepted", "completed"}


def prepare_feedback(root, cfg, goal, workflow="default", path=None, includes=(), demo=False, carry="", extra="", parent=None, model=None, budget=None,
                     ask=None, replicas=None, rounds=None):
    from .runner import prepare_run
    config, source = load_agents(root, path)
    if ask:
        config = ask_config(config, limits_from=workflow, **ask)
        workflow = "agentctl-ask"
    if workflow not in config["workflows"]:
        raise UserError("Unknown feedback workflow: " + workflow)
    if replicas is not None or rounds is not None:
        spec = config["workflows"][workflow]
        if spec["type"] != "swarm":
            raise UserError("--replicas and --rounds apply to swarm workflows only.")
        if replicas is not None:
            spec["replicas"] = replicas
        if rounds is not None:
            spec["max_rounds"] = rounds
            spec["stable_rounds"] = min(spec["stable_rounds"], rounds)
        config = validate_config(config)
    if model:
        for agent in config["agents"].values():
            if agent["backend"] == "claude-sdk":
                agent["model"] = model
    record = prepare_run(root, cfg, goal, "claude-sdk", "workspace-write", includes=includes,
                         carry=carry, extra=extra, parent=parent, model=model, budget=budget)
    frozen = Path(record["folder"]) / "agents-config.json"
    write_json(frozen, config)
    record.update(agent_workflow=workflow, workflow_type=config["workflows"][workflow]["type"], agents_config_source=str(source),
                  agents_config_sha256=digest(read_text(source)), frozen_agents_sha256=digest(read_text(frozen)),
                  agent_names=list(config["agents"]), loop_demo=demo)
    from .common import Store
    Store(root).save(record)
    return record
