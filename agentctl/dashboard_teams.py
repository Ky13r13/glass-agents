"""Validated project configuration edits for the local Team Builder."""
import json

from .agent_config import MAX_MANAGED_AGENTS, config_path, valid_name, validate_config
from .common import UserError, digest, state_dir, write_json, write_text


EDITABLE = ("backend", "model", "instructions", "write_scope", "peers")


def read_team_config(root):
    path = config_path(root)
    if not path.exists():
        return {"version": 1, "agents": {}, "tools": {}, "workflows": {}}, None
    if not path.is_file() or path.stat().st_size > 512 * 1024:
        raise UserError("Agent configuration must be a file of at most 512 KiB.")
    raw = path.read_bytes()
    return validate_config(json.loads(raw)), digest(raw)


def team_state(root):
    config, revision = read_team_config(root)
    return {
        "revision": revision,
        "max_agents": MAX_MANAGED_AGENTS,
        "agents": [{"name": name, **{field: agent.get(field) for field in EDITABLE}}
                   for name, agent in config["agents"].items()],
        "workflows": {name: {key: spec[key] for key in ("type", "agent", "worker", "reviewer") if key in spec}
                      for name, spec in config["workflows"].items()},
    }


def save_team(root, body):
    config, revision = read_team_config(root)
    if "revision" not in body or revision != body["revision"]:
        raise UserError("Agent definitions changed. Reopen Teams to reload them before saving.")
    roster, workflow = body.get("agents"), body.get("workflow")
    if not isinstance(roster, list) or not isinstance(workflow, dict) or not valid_name(workflow.get("name")):
        raise UserError("Supply a roster and a workflow name (letters, numbers, underscores or hyphens).")
    name = workflow["name"]
    previous = config["workflows"].get(name, {})
    if previous.get("type") == "swarm":
        raise UserError("Choose a different name. This workflow is a swarm.")
    agents = {}
    for row in roster:
        if not isinstance(row, dict) or not valid_name(row.get("name")) or row["name"] in agents:
            raise UserError("Agents need unique names beginning with a letter.")
        agent = config["agents"].get(row["name"], {}).copy()
        agent.update({field: row[field] for field in EDITABLE})
        agents[row["name"]] = agent
    config["agents"] = agents
    workflow_definition = {key: value for key, value in previous.items()
                           if key not in {"type", "agent", "worker", "reviewer"}}
    if workflow.get("reviewer"):
        workflow_definition.update(type="feedback", worker=workflow.get("lead"), reviewer=workflow["reviewer"])
    else:
        workflow_definition.update(type="single", agent=workflow.get("lead"))
    config["workflows"][name] = workflow_definition
    validated = validate_config(config)
    if not previous:
        pending = [workflow["lead"]] + ([workflow["reviewer"]] if workflow.get("reviewer") else [])
        reachable = set()
        while pending:
            agent = pending.pop()
            if agent not in reachable:
                reachable.add(agent)
                pending.extend(validated["agents"][agent]["peers"])
        count = len(reachable)
        if count > 12:
            validated["workflows"][name].update(max_agent_calls=3 * count, max_tool_calls=4 * count,
                                                max_turns_per_agent=2 * count, timeout_seconds=120 * count)
    if len((json.dumps(validated, indent=2, ensure_ascii=False) + "\n").encode()) > 512 * 1024:
        raise UserError("Agent configuration exceeds 512 KiB.")
    write_json(config_path(root), validated)
    # Match agents init when Glass created the default ignore file.
    ignore = state_dir(root) / ".gitignore"
    if revision is None and not ignore.is_symlink() and ignore.is_file() and ignore.read_bytes() == b"*\n":
        write_text(ignore, "*\n!agents.json\n")
    return team_state(root)
