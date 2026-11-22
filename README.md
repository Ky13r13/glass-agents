# agentctl 0.4.0

A local agent dashboard and Linux CLI for **Claude Agent SDK, Codex, OpenCode, and Ollama**. Attach agent definitions to any project, run bounded worker/reviewer feedback loops, delegate to named peers, and watch results in the dashboard. Requires **Python 3.11+ on Linux or WSL**. The original CLI/dashboard is standard-library only; the managed SDK runtime is an optional extra.

## Add agents to any project

From the extracted source folder:

~~~bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[agents]"

agentctl -C /path/to/your/project agents init
agentctl -C /path/to/your/project agents validate
agentctl -C /path/to/your/project agents run "Complete this task and revise it using independent review."
~~~

Configure provider authentication in the environment before running real SDK agents, for example ANTHROPIC_API_KEY. Review .agentctl/agents.json to set roles, per-agent backends/models, scopes, peers, typed project commands and required checks. Existing guidance and agent definitions are preserved. Python, JavaScript, mixed Codex/SDK and backtesting configurations are in examples.

The controller runs **worker → command checks → reviewer → revision**, until accepted, rejected or bounded. Failed required checks cannot be overridden by a favorable reviewer response. The default skeleton has no command gates, so its initial acceptance is advisory review.

Talk to one agent directly, within limits you set:

~~~bash
agentctl -C /path/to/your/project agents ask coder "Add CSV quoting" --in src/export
agentctl -C /path/to/your/project agents ask reviewer "Explain the import pipeline" --read-only
~~~

Or let several agents (Claude SDK, Codex, OpenCode) solve the same task in parallel. Each works in its own Git worktree; read-only critics (including a local model) judge them, and feedback rounds repeat until one candidate passes the checks, is accepted and holds the lead:

~~~bash
cp examples/agents-swarm.json /path/to/your/project/.agentctl/agents.json
agentctl -C /path/to/your/project agents run "Fix the CSV quoting bug" --workflow converge --replicas 2
agentctl -C /path/to/your/project agents apply RUN_ID   # apply the winner and rerun checks
~~~

~~~bash
# Offline scripted agents; real tools, delegation, messages and check-driven revision.
agentctl agents demo
# Offline swarm: scripted candidates in real worktrees, a scripted critic, feedback rounds, integration.
agentctl agents demo --swarm

# Watch CLI or dashboard-launched managed sessions.
agentctl -C /path/to/your/project dashboard

# Queue feedback for a named agent without starting another model call.
agentctl -C /path/to/your/project agents message RUN_ID --to worker --text "Focus on the failing test."
~~~

See [Portable agent loop guide](docs/AGENT_LOOPS.md) for single-agent tasks, swarm configuration and integration, data flow, tool input/output contracts, mixed-backend delegation, limits and SDK setup.

## Open the dashboard

From the extracted source directory, preview the interface without connecting a model:

~~~bash
python3 -m agentctl dashboard --demo
~~~

The demo creates an isolated temporary project and clearly labeled synthetic CLI activity. It does not call a model or connect market data. Close the server with Ctrl+C.

To use your actual project:

~~~bash
python3 -m agentctl -C /path/to/your/project init
python3 -m agentctl -C /path/to/your/project dashboard
~~~

The dashboard opens at **http://127.0.0.1:8765**. In **Connections**, set the path to your existing Codex or OpenCode executable, then use **New agent** to choose a CLI, model, access mode, task, and optional input files. CLI authentication is reused from your terminal environment. **Check CLI** tests executable discovery/version, not provider authentication.

Each task has live reported activity, configured provider/MCP metadata, sampled TCP connections while its process is active, output artifacts and verification evidence. Send a follow-up after a task finishes or stop it before redirecting the work. Follow-ups are fresh runs with the previous result as context. The dashboard manages the CLI processes it starts; existing interactive terminal sessions stay in their original terminals.

Read-only Codex tasks can overlap, up to the configured worker limit. Writers and all OpenCode jobs run exclusively in the workspace. OpenCode uses its own tool permissions; it does not acquire the Codex OS sandbox. No auto-approval/bypass flags are added. Headless tool approval requests may fail or reach the configured timeout; use the CLI's own interactive terminal for approval conversations.

Choose **backtest-research** in the workflow selector to design an experiment, call your existing backtester, and review actual results. It is a role template, not a replacement trading engine or a prebuilt QTE integration. Supply your actual entry point, data and fixed evaluation protocol. See [Dashboard guide](docs/DASHBOARD.md) for the input/output contract and detailed usage.

## Install

Unzip this project and run from the extracted directory:

```bash
bash install.sh
export PATH="$HOME/.local/bin:$PATH"
agentctl --help
```

The installer creates a self-contained executable at `~/.local/bin/agentctl`. It does not require pip, sudo, or network access. Keep the source folder for its documentation and tests. To update an existing installation, run `bash install.sh --replace`. To uninstall the executable, delete `~/.local/bin/agentctl`; project records remain in their respective `.agentctl` directories.

Codex and Ollama are separate runtimes; neither is bundled. You can use all context-preparation features without either runtime. For coding sessions, use your existing Codex installation and authentication.

## Your daily process

```bash
cd /path/to/your/project
agentctl init
agentctl inspect

# Preview the selected context without calling a model.
agentctl context "Add CSV export to the results screen"

# Start a normal interactive Codex session with the prepared context.
agentctl start "Add CSV export to the results screen" --interactive

# Record the meaningful next action before switching sessions.
agentctl checkpoint --note "CSV output implemented; empty input still needs checking" \
  --next "Add the empty-input case and run the export tests"

# Start a fresh, compact Codex session from that handoff.
agentctl resume --interactive
```

`resume` starts a new session with current source and a handoff. It does not replay the whole previous conversation. It preserves the prior run's read-only setting and model unless you select another backend. To resume native Codex conversation history instead, use Codex's own resume command. Scripted run records include a thread ID when Codex reports one.

Interactive sessions preserve the usual Codex terminal experience. agentctl cannot collect their live usage, final answer, or verification events; after leaving the UI, add notes with `checkpoint` and record important checks explicitly.

## Scripted runs and evidence

```bash
agentctl start "Fix the CSV quoting bug and run the relevant tests"
agentctl status
agentctl status RUN_ID

# Use the command appropriate to your project, for example:
agentctl verify RUN_ID -- python3 -m unittest discover -s tests
```

Replace `RUN_ID` with the printed ID. Scripted runs use `codex exec --json`, stream observed command summaries, store raw events, and record provider-reported token usage. They disable nested Codex subagents to keep this scheduler's concurrency predictable. They use a workspace-write sandbox for coding and a read-only sandbox for inspection. They do not bypass sandboxing or managed policy. Because scripted runs cannot answer approval prompts, commands requiring approval can fail; use interactive mode when you need to participate.

`completed` means the provider reported a completed turn with a successful process exit. It does **not** mean the task is correct. `verify` records a command, exit status, log, and source fingerprint. Checks become stale when scanned source changes. Passing checks only establish what those checks actually cover.

You can prepare first and execute later:

```bash
agentctl start "Explain the import pipeline" --read-only --prepare-only
agentctl run RUN_ID
```

Prepared runs are refused if the packet, scanned source, or observed instruction files changed. Generate a fresh packet instead. There are no automatic retries of potentially consequential actions.

## What gets created

| Location | Purpose |
|---|---|
| `AGENTS.md` | Short project guidance, created only when neither root AGENTS file already exists. |
| `.agentctl/config.toml` | Editable hardware and runtime defaults. |
| `.agentctl/REPO_MAP.md` | Generated file navigation and inferred command candidates. |
| `.agentctl/HANDOFF.md` | Human-readable continuation state and explicit next action. |
| `.agentctl/handoff.json` | Source snapshot associated with the handoff. |
| `.agentctl/state.sqlite3` | Run metadata, usage, status, and verification records. |
| `.agentctl/runs/<id>/PACKET.md` | Task-specific startup context. |
| `.agentctl/runs/<id>/manifest.json` | Included files, reasons, hashes, selected lines, and estimated tokens. |
| `.agentctl/runs/<id>/events.jsonl` | Scripted provider events. |
| `.agentctl/runs/<id>/answer.md` | Scripted agent's final answer. |
| `.agentctl/runs/<id>/stderr.log` | Provider diagnostics. |
| `.agentctl/plans/` and `.agentctl/teams/` | Generated job graphs and execution results. |
| `.agentctl/worktrees/<id>/` | Temporary swarm candidate worktrees (removed after the run unless `keep_worktrees`). |
| `.agentctl/runs/<id>/candidates/`, `winner.patch` | Swarm per-round patches and the winning or best candidate's patch. |

Runtime state is Git-ignored using `.agentctl/.gitignore`. The generated root `AGENTS.md` is a normal project file you can review and commit. Existing root guidance is preserved byte-for-byte. `inspect` recommends reusing architecture documents and creating missing ones only where justified; it does not fabricate a complete architecture from filenames or generate empty decision logs.

## Context selection

The scanner uses Git's tracked and non-ignored file list when available. It excludes common generated directories, lockfiles, binary data, oversized files, symlinks, and common credential paths. It ranks text by task terms, paths, project overviews, and build manifests. It selects bounded, line-numbered excerpts, including applicable nested instructions. It does not call an LLM to scan or plan.

For exact scope, specify the important files:

```bash
agentctl context "Investigate CSV quoting" --include src/export.py --budget 6000
```

The token estimator is UTF-8 bytes divided by three, rounded up. It is a **heuristic**, not a tokenizer or hard context/billing limit. The manifest separately reports observed global/root guidance. Codex's hidden harness, tool definitions, skills, managed policy, and profile-specific overrides cannot be fully measured by this wrapper. Actual usage is recorded when a scripted provider reports it.

Use `.agentctlignore` for additional exclusions: one filename or path glob per line, with `#` comments. Negation rules are not supported. For example:

```text
private/*
fixtures/large_*/*.json
```

Secret-path filtering and text redaction are best-effort. Inspect a packet before transmitting sensitive project content. These filters govern context preparation; they do not prevent Codex from reading other files allowed by its own sandbox. Raw local logs can contain source and command output.

## Local model setup

The optional starting profile is `qwen2.5-coder:7b-instruct-q4_K_M`, an approximately 4.7 GB model download, with 8192 context and one local request at a time. This is a conservative candidate to benchmark on your hardware, not a claim that every workload fits or performs well.

Follow [docs/LINUX_SETUP.md](docs/LINUX_SETUP.md) for driver/runtime setup, then:

```bash
agentctl doctor

# Only if Ollama is not already running; leave this terminal open.
agentctl models serve
```

In another terminal:

```bash
agentctl models setup
agentctl models benchmark
agentctl models status

cd /path/to/your/project
agentctl start "Explain the CSV export logic and likely edge cases" --backend local
```

`models setup` downloads the base model and creates `agentctl-helper:latest` with the configured context setting. `models benchmark` runs one structured code-reading smoke test and records measured generation speed. It does not certify code-editing or tool-use quality.

`--backend local` is a bounded, read-only analyst using Ollama directly. It has no shell or edit tools. It can only analyze supplied excerpts, and its output length is bounded. Hosted Codex remains the default coding agent.

An experimental `--backend codex-local --read-only` route launches Codex with a local provider. It checks whether the model advertises tool support, but that does not prove compatibility or reliability. It requires Ollama on its standard loopback port 11434. The small local context profile can be too restrictive for Codex's full tool harness; use direct local analysis as your initial workflow.

## Delegation

```bash
agentctl plan "Refactor the import pipeline across parser and storage"
# Review the printed JSON and its saved path, then:
agentctl team .agentctl/plans/PLAN_ID.json --dry-run
agentctl team .agentctl/plans/PLAN_ID.json
```

Planning is a transparent heuristic. A broad task with matches in multiple components gets up to two focused readers followed by one writer. Other tasks get one worker. You can edit the JSON to supply better task boundaries, dependencies, file includes, and write scopes. `--local-readers` on `plan` uses local analysis for the reader jobs; those requests are serialized.

The scheduler validates the graph, allows up to two read-only hosted workers to overlap, and runs writing jobs exclusively. No readers overlap a writer. Failed dependencies block dependent jobs. Results passed to a later worker are concise excerpts with artifact paths rather than whole transcripts.

In `team` graphs and the dashboard, writers share the project and are serialized. Only swarm workflows write concurrently, each candidate in a separate Git worktree; their results reach your checkout through an explicit integration step that refuses to run when the project changed. There is no automatic merging of several candidates together. `write_scope` is an instruction/audit boundary: changes outside it are detected after the run and reported, not automatically rolled back. Codex's sandbox is the execution boundary. Avoid editing the same source manually while a scripted team is running, since external changes can invalidate evidence.

## Reusable workflow agents

```bash
agentctl workflow list
agentctl workflow run bugfix --goal "Fix the CSV quoting bug" --dry-run
# Inspect the plan, then repeat without --dry-run.
```

Eight built-in workflows cover bug fixes, code review, tests, documentation, release checks, client reports, BOM audits, and quote drafts. Each supplies explicit role contracts, dependencies, and review boundaries. Client deliverables get a unique output directory. `workflow export` saves an editable job graph without launching models.

See [Workflow agents](docs/WORKFLOW_AGENTS.md) for commands and [Automation and revenue](docs/AUTOMATION_AND_REVENUE.md) for paid-service ideas, pilot economics, and optional Linux scheduling. No schedules or client delivery integrations are enabled automatically.

## Further documentation

- [Linux and GTX 1080 Ti setup](docs/LINUX_SETUP.md)
- [Architecture, data flow, and extension points](docs/ARCHITECTURE.md)
- [Configuration and command reference](docs/REFERENCE.md)
- [Implemented scope and roadmap](docs/ROADMAP.md)
- [Validation record](docs/VALIDATION.md)

Run the automated tests from this source directory:

```bash
python3 -m unittest discover -s tests -v
```
