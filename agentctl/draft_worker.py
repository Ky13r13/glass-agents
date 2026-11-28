"""Tool-free Claude and Ollama calls isolated in a cancellable Python worker."""
import asyncio
import json
from pathlib import Path
import sys

from .claude_bridge import structured_result
from .common import UserError, tokens, write_json
from .local import Ollama
from .project import redact


def activity():
    print('{"type":"planner.activity"}', flush=True)


async def claude_proposal(prompt, schema, cli_path):
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
    except ImportError as exc:
        raise UserError('Install the managed-agent dependencies with pip install ".[agents]" and restart Glass.') from exc
    options = ClaudeAgentOptions(
        tools=[], allowed_tools=[], setting_sources=[], mcp_servers={}, strict_mcp_config=True,
        max_turns=3, output_format={"type": "json_schema", "schema": schema},
        cli_path=cli_path,
        stderr=lambda line: print(redact(line), file=sys.stderr, flush=True),
    )
    result = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            activity()
            if isinstance(message, ResultMessage):
                result = message
    return structured_result(result)


def ollama_proposal(prompt, schema, cfg):
    space = cfg["local_num_ctx"] - tokens(prompt + json.dumps(schema)) - 512
    if space < cfg["local_num_predict"]:
        raise UserError("The task is too long for the configured Ollama context. Shorten it or choose another planner.")
    response = Ollama(cfg).chat(prompt, schema=schema)
    activity()
    if response.get("done_reason") == "length":
        raise UserError("Ollama reached its output limit. Ask for a smaller team or choose another planner.")
    if response.get("done") is not True:
        raise UserError("Ollama did not finish the proposal.")
    return json.loads(response["message"]["content"])


def main():
    try:
        request = json.load(sys.stdin)
        prompt, schema = request["prompt"], request["schema"]
        if request["provider"] == "claude-sdk":
            proposal = asyncio.run(claude_proposal(prompt, schema, request.get("claude_cli_path")))
        elif request["provider"] == "local":
            proposal = ollama_proposal(prompt, schema, request["config"])
        else:
            raise UserError("Unsupported draft worker provider.")
        write_json(Path(request["output"]), proposal)
        return 0
    except Exception as exc:
        print(redact(str(exc))[:4000], file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
