"""Small CLI adapters; authentication and tool execution stay in the installed CLIs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import sys
import tomllib
from urllib.parse import urlsplit, urlunsplit

from .common import UserError, run_command


def provider_argv(cfg, backend):
    if cfg.get("_demo_script"):
        return [sys.executable, "-X", "utf8", cfg["_demo_script"]]
    return [cfg[backend + "_bin"]]


def opencode_command(cfg, root, packet, permission, model=None):
    if permission != "workspace-write":
        raise UserError("OpenCode uses CLI-managed permissions, not the Codex read-only sandbox. "
                        "Select workspace-write (exclusive) or use Codex for sandboxed readers.")
    # V2 may otherwise dispatch to a shared server outside our process group.
    prefix = provider_argv(cfg, "opencode")
    help_result = run_command([*prefix, "run", "--help"], root, timeout=8)
    if not help_result or help_result.returncode:
        raise UserError("Could not inspect opencode run --help. Check the executable in Connections.")
    help_text = (help_result.stdout + help_result.stderr).decode(errors="replace")
    cmd = [*prefix, "run", "--format", "json"]
    if "--standalone" in help_text:
        cmd.append("--standalone")
    cmd += ["--agent", "build", "--file", str(packet)]
    if model:
        cmd += ["--model", model]
    # No auto-approval flags. Existing OpenCode permissions remain authoritative.
    return cmd + ["Carry out the task in the attached agentctl context packet. Do not start nested agents."]


def public_url(value):
    """Expose host/path only, never credentials, query tokens, or fragments."""
    if not isinstance(value, str) or not value:
        return None
    try:
        p = urlsplit(value)
        if p.scheme not in {"http", "https", "ws", "wss"} or not p.hostname:
            return None
        host = "[" + p.hostname + "]" if ":" in p.hostname else p.hostname
        return urlunsplit((p.scheme, host + (":" + str(p.port) if p.port else ""), p.path, "", ""))
    except ValueError:
        return None


def connection_info(root, backend, model=None, cfg=None):
    """Read only allowlisted public fields; never return an entire runtime config."""
    cfg = cfg or {}
    info = {"provider": "CLI default", "model": model or "CLI default", "endpoint": None,
            "source": "Selected model; runtime configuration may override defaults", "integrations": []}
    if backend in {"local", "codex-local"}:
        info.update(provider="Ollama", model=model or cfg.get("local_model", "CLI default"),
                    endpoint=public_url(cfg.get("ollama_url")), source="agentctl configuration")
        return info
    if backend == "claude-sdk":
        info.update(provider="Claude Agent SDK configured provider",
                    endpoint=public_url(os.environ.get("ANTHROPIC_BASE_URL")),
                    source="Managed agent definitions and SDK environment; per-agent models appear in Team")
        return info
    if backend == "codex":
        paths = [Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml",
                 root / ".codex/config.toml"]
        merged = {}
        for path in paths:
            try:
                data = tomllib.loads(path.read_text())
                for key in ("model", "model_provider", "model_providers", "mcp_servers"):
                    if key in data:
                        if isinstance(data[key], dict) and isinstance(merged.get(key), dict):
                            merged[key].update(data[key])
                        else:
                            merged[key] = data[key]
            except (OSError, ValueError):
                continue
        provider = merged.get("model_provider")
        info["provider"] = provider or "Codex configured provider"
        info["model"] = model or merged.get("model") or "CLI default"
        providers = merged.get("model_providers", {})
        selected = providers.get(provider, {}) if isinstance(providers, dict) else {}
        selected = selected if isinstance(selected, dict) else {}
        info["endpoint"] = public_url(selected.get("base_url") or os.environ.get("OPENAI_BASE_URL"))
        mcp = merged.get("mcp_servers", {})
    else:
        merged = {}
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        for path in [config_home / "opencode/opencode.json", root / "opencode.json", root / ".opencode/opencode.json"]:
            try:
                data = json.loads(path.read_text())
                for key in ("model", "provider", "providers", "mcp"):
                    if key in data:
                        if isinstance(data[key], dict) and isinstance(merged.get(key), dict):
                            merged[key].update(data[key])
                        else:
                            merged[key] = data[key]
            except (OSError, ValueError):
                continue
        selected_model = model or merged.get("model")
        info["model"] = selected_model or "CLI default"
        provider = selected_model.split("/", 1)[0] if isinstance(selected_model, str) and "/" in selected_model else None
        info["provider"] = provider or "OpenCode configured provider"
        providers = merged.get("providers", merged.get("provider", {}))
        selected = providers.get(provider, {}) if isinstance(providers, dict) else {}
        options = selected.get("settings", selected.get("options", {})) if isinstance(selected, dict) else {}
        options = options if isinstance(options, dict) else {}
        info["endpoint"] = public_url(options.get("baseURL") or options.get("baseUrl") or options.get("base_url"))
        mcp = merged.get("mcp", {})
        if isinstance(mcp, dict) and isinstance(mcp.get("servers"), dict):
            mcp = mcp["servers"]
        info["source"] = "Selected model and readable JSON config; JSONC, managed settings and profiles may differ"
    if isinstance(mcp, dict):
        for name, server in list(mcp.items())[:30]:
            if not isinstance(server, dict):
                continue
            command = server.get("command")
            executable = command[0] if isinstance(command, list) and command else command if isinstance(command, str) else None
            info["integrations"].append({"name": str(name), "endpoint": public_url(server.get("url")),
                                         "executable": executable, "state": "configured, not probed"})
    return info


def process_connections(pid):
    """Linux-only socket sample for the owned process and its current descendants."""
    if not pid or not Path("/proc").is_dir():
        return []
    pending, pids, inodes = [int(pid)], set(), set()
    while pending and len(pids) < 128:
        current = pending.pop()
        if current in pids:
            continue
        pids.add(current)
        base = Path("/proc") / str(current)
        try:
            if base.stat().st_uid != os.getuid():
                continue
            try:
                pending.extend(int(x) for x in (base / "task" / str(current) / "children").read_text().split())
            except (OSError, ValueError):
                pass
            for fd in (base / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                    match = re.fullmatch(r"socket:\[(\d+)\]", target)
                    if match:
                        inodes.add(match.group(1))
                except OSError:
                    pass
        except (OSError, ValueError):
            continue
    found = set()
    for current in pids:
        for table in ("tcp", "tcp6"):
            try:
                rows = (Path("/proc") / str(current) / "net" / table).read_text().splitlines()[1:]
                for row in rows:
                    fields = row.split()
                    if len(fields) < 10 or fields[9] not in inodes or fields[3] != "01":
                        continue
                    address, port = fields[2].split(":")
                    raw = bytes.fromhex(address)
                    if table == "tcp":
                        ip = socket.inet_ntop(socket.AF_INET, raw[::-1])
                    else:
                        raw = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
                        ip = socket.inet_ntop(socket.AF_INET6, raw)
                    found.add((ip, int(port, 16)))
            except (OSError, ValueError):
                pass
    return [{"address": ip, "port": port, "state": "established"} for ip, port in sorted(found)][:40]


FLOW_FIELDS = ("actor", "agent", "candidate", "parent", "recipient", "tool", "slot", "call_id",
               "decision", "leader", "qualified", "outcome", "status", "checks_pass", "backend", "model")


def flow_fields(event):
    """Short, typed routing fields the dashboard uses to draw who talks to whom."""
    kind = str(event.get("type", ""))
    flow = {"type": kind.removeprefix("agentctl.")}
    for key in FLOW_FIELDS:
        value = event.get(key)
        if isinstance(value, (bool, int, float)) or (isinstance(value, str) and len(value) <= 200):
            flow[key] = value
    review = event.get("review")
    if isinstance(review, dict) and isinstance(review.get("decision"), str):
        flow["decision"] = review["decision"]
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("decision"), str):
        flow["decision"] = response["decision"]
    return flow


def event_summary(event):
    """Provider events become display data only, never HTML or executable commands."""
    kind = str(event.get("type", "unknown"))
    if kind.startswith("agentctl."):
        value = agentctl_summary(event, kind)
        value["flow"] = flow_fields(event)
        return value
    return provider_summary(event, kind)


def agentctl_summary(event, kind):
    actor = str(event.get("actor", "controller"))
    if kind == "agentctl.peer.event":
        value = dict(event.get("event", {}))
        value["label"] = actor + " · " + value.get("label", "Peer activity")
        return value
    label = actor + " · " + kind.removeprefix("agentctl.").replace(".", " ")
    if kind == "agentctl.agent.message":
        return {"kind": "message", "label": label, "text": str(event.get("text", ""))}
    if kind == "agentctl.review.decision":
        return {"kind": "message", "label": label,
                "text": json.dumps({"review": event.get("review"), "checks_pass": event.get("checks_pass"),
                                    "accepted": event.get("accepted")}, indent=2)}
    if kind in {"agentctl.tool.started", "agentctl.tool.requested", "agentctl.tool.completed",
                "agentctl.project_tool.started", "agentctl.project_tool.completed"}:
        value = event.get("input", event.get("argv", event.get("result", "")))
        return {"kind": "tool", "label": actor + " · " + str(event.get("tool", "")),
                "text": value if isinstance(value, str) else json.dumps(value, indent=2)[:16000],
                "status": kind.rsplit(".", 1)[-1]}
    if kind == "agentctl.message.sent":
        return {"kind": "message", "label": actor + " → " + str(event.get("recipient")),
                "text": str(event.get("message", ""))}
    if kind == "agentctl.revision.requested":
        return {"kind": "message", "label": label, "text": str(event.get("feedback", ""))[:16000]}
    if kind.endswith(".failed") or kind == "agentctl.scope.violation":
        return {"kind": "error", "label": label, "text": str(event.get("error", event.get("paths", "")))}
    if kind == "agentctl.swarm.round":
        return {"kind": "status", "label": label, "text": str(event.get("summary", ""))}
    if kind == "agentctl.candidate.dropped":
        return {"kind": "error", "label": label, "text": str(event.get("reason", ""))}
    return {"kind": "status", "label": label, "text": str(event.get("outcome", event.get("task", "")))}


def provider_summary(event, kind):
    item = event.get("item", event.get("part", {}))
    if not isinstance(item, dict):
        item = {}
    subtype = item.get("type", "")
    if subtype in {"agent_message", "text"} or kind == "text":
        return {"kind": "message", "label": "Agent message", "text": str(item.get("text", ""))}
    if subtype == "command_execution":
        return {"kind": "tool", "label": "Shell command", "text": str(item.get("command", "")),
                "detail": str(item.get("aggregated_output", "")), "status": item.get("status")}
    if kind == "tool_use" or subtype in {"tool", "mcp_tool_call", "web_search", "file_change"}:
        state = item.get("state", {})
        state = state if isinstance(state, dict) else {}
        label = str(item.get("tool", item.get("tool_name", subtype))) or "Tool"
        text = state.get("input", item.get("arguments", item.get("changes", item)))
        return {"kind": "tool", "label": label, "text": text if isinstance(text, str) else json.dumps(text),
                "detail": str(state.get("output", item.get("result", "")))[:10000],
                "status": state.get("status", item.get("status"))}
    if kind in {"error", "turn.failed"}:
        return {"kind": "error", "label": "Runtime error", "text": json.dumps(event.get("error", event))}
    if "completed" in kind or kind == "step_finish":
        return {"kind": "status", "label": kind.replace(".", " "), "text": ""}
    return {"kind": "status", "label": kind.replace(".", " "), "text": ""}
