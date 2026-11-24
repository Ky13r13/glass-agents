# Portable managed agents and feedback loops

Each project supplies named agents, typed project commands and bounded workflows. Glass routes peer tasks, results and messages between Claude SDK, Codex, OpenCode and local agents. Backtesting is an example configuration.

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

The `peers` array grants outbound permission by name. To allow notes in both directions, list each agent in the other's definition. Sending a message queues it for the recipient. Calling a peer starts a managed invocation and delivers its result to the caller. A peer can send a note back to its active caller. Glass checks delegation depth, rejects recursive calls, and requires a delegated writer's scope to fit inside the caller's scope before launch.

Claude SDK agents use the tools above. A Codex, OpenCode, local Ollama or codex-local agent with assigned peers can return one structured operation per turn. For example, `{"turn":{"call_agent":{"agent":"advisor","task":"Check the CSV interface."}}}` requests a peer. Glass validates each request, executes it, and starts a fresh provider turn with recent addressed mail and previews of the last four exchanges. The agent finishes with `final` containing its usual worker result or review decision. Each native turn has a linked run with the provider's output and reported usage. Full peer requests and results are recorded in the controller events.

In `examples/agents-mixed.json`, the default workflow starts with the Claude SDK coordinator. Choose `--workflow codex-led` to start with the Codex coder, which consults a Claude SDK advisor before the check and review. Select each agent's backend and optional model in the project configuration. Each runtime uses its existing authentication.

## One agent, one task

~~~bash
agentctl -C /path/to/project agents ask coder "Add CSV quoting" --in src/export
agentctl -C /path/to/project agents ask reviewer "Explain the import pipeline" --read-only
agentctl -C /path/to/project agents ask worker "Fix the parser" --scope "src/parser/**" --check tests
~~~

ask runs one named agent from agents.json once, followed by any --check tools. It has no reviewer and no revision loop. The outcome is completed when the agent reported a result and every check passed. Otherwise it is checks_failed, failed or limit_reached.

Permissions can only be narrowed:

- --in FOLDER sets the write scope to FOLDER/** and tells the agent to stay there.
- --scope GLOB sets an explicit narrower scope. It must lie inside the agent's configured write_scope.
- --read-only removes write access and drops writing peers.

Writing peers keep scopes covered by both their configuration and the narrowed caller. The task's limits come from the default workflow when one exists.

## Swarm: many agents converge on one solution

A swarm workflow runs several candidate agents on the same goal at the same time. Each candidate gets its own Git worktree, so concurrent writers never share files.

~~~mermaid
flowchart TD
    B["Baseline: current working tree"] --> W1["Candidate 1 worktree"]
    B --> W2["Candidate 2 worktree"]
    B --> WN["Candidate N worktree"]
    W1 & W2 & WN --> C["Required checks in each worktree"]
    C --> F["Finalists: candidates whose checks pass"]
    F --> K["Read-only critics: decision + 0-10 score"]
    K --> L{"Leader stable, checks pass, enough accepts?"}
    L -->|"no"| R["Feedback: own checks, critiques, leader's diff"]
    R --> W1 & W2 & WN
    L -->|"yes"| A["Winner patch + review branch"]
    A -->|"apply_winner or agents apply"| P["Your working tree + checks rerun"]
~~~

~~~bash
cp examples/agents-swarm.json /path/to/project/.agentctl/agents.json
agentctl -C /path/to/project agents validate
agentctl -C /path/to/project agents run "Fix the CSV quoting bug" --workflow converge
agentctl -C /path/to/project agents run "Fix the CSV quoting bug" --workflow converge --replicas 3 --rounds 4

# Offline: three scripted candidates and a scripted critic in real worktrees.
agentctl agents demo --swarm
~~~

Each round works like this:

1. Every active candidate works in its own worktree, up to max_parallel at a time.
2. The required checks run in each worktree.
3. Candidates whose checks pass become finalists, up to max_finalists. Without required checks, finalists rotate each round.
4. Every critic judges each finalist inside that finalist's worktree.
5. Ranking order is: checks pass, then number of critic accepts, then mean score, then fewest failed checks. Ties keep the current leader.
6. The swarm accepts when the leader passes its checks, has at least min_accepts critic accepts, and has led for stable_rounds consecutive rounds.

Otherwise every candidate receives its own check evidence, critic feedback, the leader's summary and a truncated diff, then revises. In mode adopt_leader, the non-leaders' worktrees are first reset to the leader's code.

| Field | Default | Meaning |
|---|---|---|
| candidates | required | Writer agents (claude-sdk, codex or opencode) with a write_scope. |
| critics | required | Read-only agents that judge finalists. A local backend is allowed. |
| replicas | 1 | Copies of each candidate. candidates × replicas is at most 16. |
| max_rounds / stable_rounds | 3 / 1 | Round limit, and how many consecutive rounds the accepted leader must hold. |
| max_parallel / max_finalists / min_accepts | 4 / 3 / 1 | Concurrency, critic workload per round, and acceptance threshold. |
| mode | independent | independent: each candidate revises its own code. adopt_leader: non-leaders restart from the leader's code. |
| apply_winner | false | Apply an accepted winner to your working tree and rerun the checks there. |
| keep_worktrees | false | Keep candidate worktrees after the run for inspection. |

Limits are counted in two places:

- timeout_seconds and max_reported_cost_usd cover the whole swarm.
- max_agent_calls, max_tool_calls, max_depth and max_turns_per_agent apply to each candidate workspace. Critic calls count against the candidate being judged.

A candidate that fails or exhausts its own limits is dropped, without an automatic retry. The swarm keeps going with the other candidates.

### Isolation and integration

- **Baseline:** the starting point is a commit of your current working tree, including uncommitted and untracked non-ignored files. It is built with a temporary index, so your branch, index and files are untouched. Git hooks are disabled for every worktree operation. The project must be a Git repository; it does not need any commits.
- **Records:** each candidate's work is committed in its worktree after every round. Its changed files are checked against the agent's write_scope at the Git level, which also catches binary and unscanned files.
- **After the run:** round and final patches remain under the run's candidates/ folder. The winner, or the best unaccepted candidate, is saved as winner.patch and kept on a review branch named agentctl/swarm/RUN_ID. Worktrees are removed unless keep_worktrees is true.
- **Integration:** this happens only through apply_winner or agents apply. It refuses to run if scanned project files changed since the swarm started. It applies the patch to your working tree only (nothing is staged or committed) and reruns the required checks there.
- **Evidence:** only those reruns are recorded as the run's verification evidence. Checks inside candidate worktrees are kept in the swarm state, but they are not evidence about your checkout.

~~~bash
agentctl -C /path/to/project agents apply RUN_ID
agentctl -C /path/to/project agents apply RUN_ID --candidate claude_thorough --allow-unaccepted
~~~

Operator messages sent with `agents message RUN_ID --to AGENT` reach every copy of that agent at the start of its next round.

### Costs and limits of a swarm

- **Cost:** N candidates multiplied by rounds, plus critics multiplied by finalists, adds up quickly. Codex and OpenCode candidates often report no cost, so the cost limit is an estimate that can miss spending. Start with 2–3 candidates and 2–3 rounds.
- **Diversity:** identical replicas tend to agree with each other. Different models or different candidate instructions give the critics real alternatives to choose between.
- **Local critics:** they are serialized behind the per-user local-model lock. They see a bounded excerpt of the critique request, not the full diff.
- **Correctness:** acceptance means checks passed, critics accepted and the leader held the lead. It does not certify correctness beyond what the checks test.

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

Choose **Managed · default** (or a swarm workflow) under Workflow when launching. The communication map draws each agent, swarm candidate and critic, with arrows for tasks, results, messages, reviews and tool calls; see [Dashboard](DASHBOARD.md#communication-map). **Details** lists each call's agent, backend, model, parent, task and status. **Activity** shows tools, messages and review decisions. Results includes outcome, iterations, check evidence and loop state/message artifacts.

CLI-launched managed workflows also appear for monitoring. While active, select a named recipient beside the instruction box to queue feedback. Stop/resume a CLI-launched run in its terminal; dashboard-owned runs have the Stop control. Other jobs wait for the workflow's exclusive project lock.

## Reusable configurations

| File | Starting point |
|---|---|
| examples/agents-python.json | SDK roles and unittest gates. |
| examples/agents-node.json | SDK roles and npm test gates. |
| examples/agents-mixed.json | Claude SDK or Codex leads, with two-way peer messages and Python test gates. |
| examples/agents-backtesting.json | Typed development backtest adapter and fixed protocol context. |
| examples/agents-swarm.json | Two Claude SDK candidates with different styles plus a Codex candidate, judged by a Claude reviewer and a local critic. |

Copy an example to .agentctl/agents.json, adapt commands/scopes and run agents validate. The backtesting example contains placeholders: connect your actual engine and data. It supplies no strategy/data/optimizer, and its example has advisory review until you add your own objective/robustness check gates. Use an adapter/data-access boundary when a holdout dataset must be inaccessible to research agents.

Optional external MCP definitions can be placed in mcp_servers. Assign server names and explicit fully qualified mcp_tools to individual SDK agents. Only configured servers are loaded; implicit user/project MCP discovery is disabled.

## Limits and lifecycle

Each workflow defines max_iterations, max_agent_calls, max_tool_calls, max_depth, timeout_seconds, max_turns_per_agent and max_reported_cost_usd.

- Each delegation consumes a managed agent call. Each peer request consumes a managed tool call. `max_turns_per_agent` bounds the number of native provider launches for one managed invocation, including its final response. CLI-internal tool use stays under the CLI's own policies.
- Reported cost is a client estimate, can overshoot on a call, and can be unavailable for native peers. Missing usage/cost is unreported rather than zero.
- SDK custom file tools enforce paths/scopes. Registered commands and MCP servers are trusted capabilities running under their host/runtime permissions. This wrapper is not a general OS sandbox.
- Writers stay exclusive in one checkout. Swarm candidates write concurrently only in separate Git worktrees; the user's checkout changes only through the explicit, freshness-checked integration step. Audits cover scanned text and exclude documented ignored/generated/oversized/binary paths; they do not automatically roll back changes.
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
