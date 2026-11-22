# Portable managed agents and feedback loops

Version 0.4 adds a project-agnostic controller. Each project supplies named agents, typed project commands and a bounded worker/reviewer workflow. Claude Agent SDK agents can call assigned peers, including managed Codex and OpenCode processes. Backtesting is an example configuration.

## Install once, attach to a project

Use Python 3.11+ on Linux or WSL. From the extracted agentctl source directory:

~~~bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[agents]"
~~~

The optional agents extra installs Claude Agent SDK and jsonschema. The original CLI/dashboard still works without those extras. The SDK bundles its Claude Code runtime. Set provider authentication in the environment before making model calls, for example:

~~~bash
export ANTHROPIC_API_KEY="your-key"
~~~

Keep credentials out of project configuration and version control.

~~~bash
agentctl -C /path/to/any-project agents init
agentctl -C /path/to/any-project agents validate
agentctl -C /path/to/any-project agents run "Complete the task and revise using independent review."
~~~

Initialization creates the normal helpers if needed, then .agentctl/agents.json. Existing project guidance and agent definitions are preserved. Default roles are worker, reviewer and advisor, independent of project language. The worker has write scope; reviewer/advisor are readers. No command checks are configured in the skeleton, so initial acceptance is advisory review until you add project gates.

The generated state-directory ignore file allows agents.json to be tracked while runtime files remain ignored. Custom ignore files are preserved. Parent ignore rules can still require explicitly adding the configuration when committing it.

## An actual feedback loop

~~~mermaid
flowchart TD
    W["Worker agent"] --> T["Project tools and managed peers"]
    T --> C["Required command checks"]
    C --> R["Reviewer agent"]
    R -->|"revise"| W
    R -->|"accept and checks pass"| A["Accepted result"]
    R -->|"reject"| X["Stopped result"]
~~~

Each iteration runs the worker, records actual tool results, runs required checks, then starts an independent reviewer. The controller validates the review JSON: accept, revise or reject. Accept requires passing tool contracts/checks, a current scanned-source fingerprint and no observed scope violation. Failed checks cannot be overridden by reviewer prose.

On revise, the controller sends feedback plus measured check evidence to the worker and starts the next iteration. Invalid decisions, execution errors, rejection and configured limits stop the run. Provider completion, workflow acceptance and correctness remain distinct.

## Agent interaction

| SDK tool | Purpose |
|---|---|
| list_agents | Describe the caller's assigned peers. |
| call_agent | Delegate a focused task to a named managed peer and receive its result. |
| send_message / read_messages | Exchange addressed project-local messages. |
| list_tool_results / get_tool_result | Share recorded command evidence across agents and iterations. |
| list_files / read_file | Read bounded, filtered project text. |
| write_file | Write within assigned paths; available only to writers. |
| project_NAME | Invoke a specifically assigned, typed project command. |

The peers array controls delegation and peer messaging. Recursive delegation and excess depth/call counts are blocked. Peer results include execution status and evidence, not a guarantee of correctness. The caller's audit scope must encompass authorized delegated writes.

Claude SDK agents receive these custom tools. Native Codex/OpenCode/local peers run through the existing process runner and return task results; they keep their own native tools/policies. Codex peers receive an output schema and other peer responses are validated by the controller. Existing terminal sessions are not attached.

See examples/agents-mixed.json for an SDK coordinator delegating to a Codex coder. Set an explicit backend, optional model, instructions, write_scope, peers and project_tools for each agent.

## Project command inputs and outputs

Register fixed executable argument arrays under tools. Assign their names to an agent's project_tools to let it invoke them. Assign a tool to the workflow's required_checks to run it after every worker iteration. Required checks must accept empty input.

Example tool entry:

~~~json
{
  "tests": {
    "description": "Run the existing Python tests",
    "argv": ["python3", "-m", "unittest", "discover", "-s", "tests"],
    "input_schema": {"type": "object", "additionalProperties": false},
    "format": "text",
    "timeout_seconds": 120
  }
}
~~~

Use your project's own commands: npm test, ctest --test-dir build --output-on-failure, a validation script, or an existing analysis CLI. Initialization/validation never runs inferred scripts.

A registered command receives this envelope on stdin:

~~~json
{
  "version": 1,
  "tool": "evaluate",
  "input": {"lookback": 60},
  "context": {"dataset_id": "development_v1", "protocol_id": "fixed_v1"},
  "project_root": "/path/to/project",
  "output_dir": "/path/to/project/.agentctl/runs/RUN_ID/loop/tools/CALL_ID"
}
~~~

The agent supplies input; the command definition supplies context. Use fixed context for dataset/split/cost/benchmark settings that should stay outside adjustable strategy parameters.

For existing commands that do not consume JSON stdin, use entire-argument placeholders:

~~~json
{
  "argv": ["python3", "evaluate.py", "--lookback", "{input.lookback}"],
  "input_schema": {
    "type": "object",
    "properties": {"lookback": {"type": "integer", "minimum": 10, "maximum": 200}},
    "required": ["lookback"],
    "additionalProperties": false
  }
}
~~~

A value remains one literal argument. The wrapper does not evaluate shell syntax. Nested fields can use {input.parameters.lookback}; static paths can use {project_root} and {output_dir}. cwd is an optional relative project directory.

Choose format=text for ordinary output or format=json for adapters. Add output_schema to JSON tools to validate output; an invalid result fails its contract even with process exit zero. Schemas must be self-contained. Write large reports/tables/plots to output_dir and return compact summaries/artifact references.

For every command, the controller retains input.json, stdout, stderr and result.json. Required checks also appear in the normal verification evidence and freshness checks.

## Prepare, monitor and send feedback

~~~bash
agentctl -C /path/to/project agents run "Your goal" \
  --workflow default --include README.md --prepare-only
agentctl -C /path/to/project run RUN_ID

agentctl -C /path/to/project agents message RUN_ID \
  --to worker --text "Focus the next revision on the failing import test."
agentctl -C /path/to/project agents messages RUN_ID --to worker
agentctl -C /path/to/project agents events RUN_ID --follow
~~~

Preparation reads files and freezes configuration; it does not call models, start peers/MCP servers or run project commands. Changes to source, guidance, agent definitions or the frozen config invalidate prepared runs.

Messages are read on the recipient's next message-tool call or invocation; they do not interrupt an in-flight model response. These are local managed-agent mailboxes, not messages to people or remote services.

The existing start/context/resume routes also work:

~~~bash
agentctl -C /path/to/project start "Your goal" --backend claude-sdk
agentctl -C /path/to/project resume "Continue with this feedback"
~~~

Use --agent-workflow or --agents-config for an alternative workflow or project-local configuration. A --model override applies to all SDK agents in that run; otherwise per-agent models are retained.

## Dashboard

~~~bash
agentctl -C /path/to/project dashboard
~~~

Choose **Managed feedback · default** when launching. **Managed team** shows each call's agent, backend, model, parent, task and status. Live activity shows tools, messages and review decisions. Results includes outcome, iterations, check evidence and loop state/message artifacts.

CLI-launched managed workflows also appear for monitoring. While active, select a named recipient beside the instruction box to queue feedback. Stop/resume a CLI-launched run in its terminal; dashboard-owned runs have the Stop control. Other jobs wait for the workflow's exclusive project lock.

## Reusable configurations

| File | Starting point |
|---|---|
| examples/agents-python.json | SDK roles and unittest gates. |
| examples/agents-node.json | SDK roles and npm test gates. |
| examples/agents-mixed.json | SDK coordinator, Codex coder and Python test gates. |
| examples/agents-backtesting.json | Typed development backtest adapter and fixed protocol context. |

Copy an example to .agentctl/agents.json, adapt commands/scopes and run agents validate. The backtesting example contains placeholders: connect your actual engine and data. It supplies no strategy/data/optimizer, and its example has advisory review until you add your own objective/robustness check gates. Use an adapter/data-access boundary when a holdout dataset must be inaccessible to research agents.

Optional external MCP definitions can be placed in mcp_servers. Assign server names and explicit fully qualified mcp_tools to individual SDK agents. Only configured servers are loaded; implicit user/project MCP discovery is disabled.

## Limits and lifecycle

Each workflow defines max_iterations, max_agent_calls, max_tool_calls, max_depth, timeout_seconds, max_turns_per_agent and max_reported_cost_usd.

- Iterations, managed calls, wrapper/SDK tool calls, depth and runtime are enforced in code. Native CLI peers have their own inner tool loops; those individual native calls are not counted in the wrapper's tool counter.
- Reported cost is a client estimate, can overshoot on a call, and can be unavailable for native peers. Missing usage/cost is unreported rather than zero.
- SDK custom file tools enforce paths/scopes. Registered commands and MCP servers are trusted capabilities running under their host/runtime permissions. This wrapper is not a general OS sandbox.
- Writers stay exclusive in one checkout. Audits cover scanned text and exclude documented ignored/generated/oversized/binary paths; they do not automatically roll back changes.
- Ctrl+C cancels a CLI run. The parent runner bounds time/output, drains both pipes and terminates process groups; the managed worker propagates stop requests to SDK sessions, commands and CLI peers.
- Interrupted runs are not automatically relaunched. CLI exit 0 requires accepted; rejection, limits and errors return nonzero. Stored execution status can be completed while loop_outcome is rejected or limit_reached.

## Offline demo and validation

~~~bash
agentctl agents demo
agentctl agents demo --output /tmp/my-agent-feedback-demo
~~~

The demo uses scripted agent fixtures with real tools, a real failing/passing check, peer delegation, messages and persistence. The reviewer requests revision because of a failed check, and the worker changes its next action from that feedback. No model/external service is called. jsonschema is required; the SDK is not needed for the scripted demo.

The adapter was also checked against installed Claude SDK 0.2.187 for options, actual custom MCP tool registration and stream/result normalization with an offline client fixture. Authenticated provider sessions were not run. See VALIDATION.md.

Official SDK references: [Agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop), [Python](https://code.claude.com/docs/en/agent-sdk/python), [Custom tools](https://code.claude.com/docs/en/agent-sdk/custom-tools), [Structured outputs](https://code.claude.com/docs/en/agent-sdk/structured-outputs), [Hooks](https://code.claude.com/docs/en/agent-sdk/hooks).
