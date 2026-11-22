"""Optional Claude Agent SDK integration; the core CLI does not import the SDK."""
from __future__ import annotations

import asyncio
import contextlib
import json
import math

from .common import UserError, read_text
from .agent_runtime import clean


async def sdk_call(runtime, actor, prompt, stack, schema):
    try:
        from claude_agent_sdk import (
            ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, PermissionResultAllow,
            PermissionResultDeny, ResultMessage, tool, create_sdk_mcp_server,
        )
    except ImportError as exc:
        raise UserError("Claude Agent SDK is not installed in this Python environment. From the agentctl source folder run: python3 -m pip install '.[agents]'") from exc
    agent = runtime.agent_spec(actor)
    sdk_tools = []
    for name, (description, input_schema) in runtime.tool_definitions(actor).items():
        def make_handler(action):
            async def handler(arguments):
                try:
                    result = await runtime.tool(actor, action, arguments, stack)
                    raw = json.dumps(clean(result), ensure_ascii=False)
                    if len(raw) > 28000:
                        raw = json.dumps({"preview": raw[:24000], "truncated": True,
                                          "note": "Inspect recorded tool evidence and artifact references for full output."})
                    return {"content": [{"type": "text", "text": raw}]}
                except Exception as exc:
                    return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
            return handler
        sdk_tools.append(tool(name, description, input_schema)(make_handler(name)))
    server = create_sdk_mcp_server(name="agentctl", version="0.4.0", tools=sdk_tools)
    servers = {name: runtime.config["mcp_servers"][name] for name in agent["mcp_servers"]}
    servers["agentctl"] = server
    allowed = ["mcp__agentctl__" + n for n in runtime.tool_definitions(actor)] + agent["mcp_tools"]

    async def permission(name, arguments, context):
        if name not in allowed and name != "StructuredOutput":
            return PermissionResultDeny(message="This tool is not assigned to this managed agent.")
        return PermissionResultAllow(updated_input=arguments)

    async def before_tool(data, tool_use_id, context):
        name = data.get("tool_name", "")
        if name == "StructuredOutput":
            return {}
        try:
            runtime.budget()
            if name not in allowed:
                raise UserError("Tool is not assigned to this agent.")
            if not name.startswith("mcp__agentctl__"):
                runtime.reserve_tool()
                runtime.emit("tool.started", actor, tool=name, input=data.get("tool_input"))
        except UserError as exc:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": str(exc)}}
        return {}

    async def after_tool(data, tool_use_id, context):
        name = data.get("tool_name", "")
        if name != "StructuredOutput" and not name.startswith("mcp__agentctl__"):
            runtime.emit("tool.completed", actor, tool=name,
                         result=str(data.get("tool_response", data.get("error", "")))[:16000])
        return {}

    guidance = ""
    for filename in ("AGENTS.override.md", "AGENTS.md"):
        path = runtime.root / filename
        if path.is_file() and not path.is_symlink():
            if path.stat().st_size > runtime.cfg["max_file_bytes"]:
                raise UserError("Root project guidance exceeds the configured file limit.")
            guidance = "\n\nApplicable root project guidance:\n" + read_text(path)
            break
    remaining_cost = max(0.001, runtime.remaining_cost())
    kwargs = {
        "tools": [], "mcp_servers": servers, "strict_mcp_config": True,
        "allowed_tools": allowed, "can_use_tool": permission,
        "setting_sources": [], "cwd": str(runtime.root),
        "system_prompt": agent["instructions"] + guidance,
        "max_turns": runtime.workflow["max_turns_per_agent"], "max_budget_usd": remaining_cost,
        "output_format": {"type": "json_schema", "schema": schema},
        "hooks": {"PreToolUse": [HookMatcher(hooks=[before_tool])],
                  "PostToolUse": [HookMatcher(hooks=[after_tool])],
                  "PostToolUseFailure": [HookMatcher(hooks=[after_tool])]},
        "stderr": lambda text: runtime.emit("sdk.diagnostic", actor, text=str(text)[:8000]),
    }
    if agent.get("model"):
        kwargs["model"] = agent["model"]
    if runtime.config.get("claude_cli_path"):
        kwargs["cli_path"] = runtime.config["claude_cli_path"]
    options = ClaudeAgentOptions(**kwargs)
    owner = asyncio.current_task()
    result = None
    async with ClaudeSDKClient(options=options) as client:
        async def watch_stop():
            while not runtime.stop.is_set():
                await asyncio.sleep(0.1)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.interrupt(), timeout=1)
            owner.cancel()
        watcher = asyncio.create_task(watch_stop())
        try:
            await client.query(prompt)
            async for message in client.receive_response():
                runtime.budget()
                if isinstance(message, ResultMessage):
                    result = message
                    continue
                data = getattr(message, "data", {})
                if getattr(message, "subtype", None) == "init" and isinstance(data, dict):
                    runtime.emit("agent.session", actor, session_id=data.get("session_id"), model=data.get("model"),
                                 mcp_servers=[{"name": s.get("name"), "status": s.get("status")}
                                              for s in data.get("mcp_servers", []) if isinstance(s, dict)])
                for block in getattr(message, "content", []) or []:
                    # Never dump hidden reasoning/signatures or entire SDK configuration objects.
                    if type(block).__name__ == "TextBlock":
                        runtime.emit("agent.message", actor, text=block.text[:16000])
                    elif type(block).__name__ == "ToolUseBlock" and block.name != "StructuredOutput":
                        runtime.emit("tool.requested", actor, tool=block.name, input=block.input)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
    if result is None:
        raise UserError("Claude SDK ended without a result message.")
    usage = getattr(result, "usage", None) or {}
    if isinstance(usage, dict):
        for key, value in usage.items():
            if type(value) in {int, float} and math.isfinite(value):
                runtime.state["usage"][key] = runtime.state["usage"].get(key, 0) + value
    cost = getattr(result, "total_cost_usd", None)
    if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0:
        runtime.state["reported_cost_usd"] += cost
        runtime.state["cost_reports"] += 1
    else:
        runtime.state["cost_coverage"] = "partial: one or more calls omitted a cost estimate"
    runtime.emit("agent.usage", actor, usage=usage, reported_cost_usd=cost,
                 session_id=getattr(result, "session_id", None))
    if (getattr(result, "is_error", False) or getattr(result, "subtype", "") != "success"
            or getattr(result, "stop_reason", None) == "refusal"
            or getattr(result, "terminal_reason", None) not in {None, "completed"}):
        raise UserError("Claude SDK did not complete successfully: " +
                        str(getattr(result, "errors", None) or getattr(result, "result", None) or getattr(result, "subtype", "unknown")))
    structured = getattr(result, "structured_output", None)
    if structured is None:
        try:
            structured = json.loads(getattr(result, "result", "") or "")
        except ValueError as exc:
            raise UserError("Claude SDK did not return the required structured result.") from exc
    return structured
