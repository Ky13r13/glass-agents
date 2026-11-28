# Agent dashboard

**Teams** edits the project's existing agent definitions and single-lead or feedback workflows. Describe the task, pick **Draft with**, select the eligible team runtimes, and use **Draft team**. The drafting provider receives the task in an isolated working directory and leaves project files alone. **Start a new project** includes scaffolding instructions for the approved run. The planner can propose up to 200 agents within the project's total 250-agent limit. Team members run as invoked by the lead, reviewer, or a permitted peer. Large rosters do not imply simultaneous execution.

The draft progress area reports startup, drafting, and validation for the selected provider. Its timer measures elapsed time and tracks the most recent provider event. **Cancel draft** stops the planner. A draft that reaches its time limit displays an error and can be retried.

The draft needs the optional managed-agent dependencies and a working runtime for the selected provider. Codex uses the configured CLI, Claude Code uses the Claude Agent SDK with its existing authentication, and Ollama uses the project's configured local model. For Ollama, smaller teams fit more readily into the configured output limit. Review the roles and write scopes before saving and launching. Use **Can contact** to grant outgoing message and delegation access. Queued messages are available on the recipient's next invocation or mailbox read.

Choose a run from the **Runs** list to open its communication map. On wide screens the list sits to the right of the map. **Activity**, **Results** and **Details** sit below the map. The top bar holds navigation, live counts and **New agent**.

## Communication map

Each agent appears as a glass droplet with eyes. The field backdrop and four droplet materials are rendered from Vitreum’s magnetic visualizer shaders and bundled for offline use. An agent keeps its material as its status changes. Running agents breathe and blink, completed agents close their eyes, and failed agents look worried. Move the pointer over a droplet to draw it gently toward you. Select it with a click, Enter or Space to see its status, model, recorded task and incoming peer messages. **Show all activity** clears the selection.

During a live managed feedback run, **Write message** opens the composer addressed to the selected agent. Press Send to queue the message. Scroll the map to reach additional agents in larger teams. The dashboard respects the system's reduced-motion preference.

Arrows run from the sender to the recipient.

| Arrow | Meaning |
|---|---|
| task (olive) | You, the controller or a delegating agent handing work to an agent. Feedback revisions are drawn as tasks too. |
| result (teal) | An agent's reply returning to whoever asked. |
| message (plum) | A mailbox message from one agent to another. |
| review (ochre) | A swarm candidate sent to a critic, or a reviewer's verdict going back to the controller. |
| tool (grey) | File access, project commands such as tests, and MCP or CLI tools. |
| model / network (dashed blue) | Calls to the model provider; for single CLI runs, live TCP connections sampled from the process. |

How it behaves:

- **Live traffic** travels along animated arrows. A star (★) marks the current swarm leader.
- **After the run:** a finished run keeps every arrow. Tool and model arrows fade so the agent-to-agent paths stand out.
- Hover over an arrow for its count and latest content.

Arrows come only from recorded events: managed-agent events, CLI tool events and sampled sockets. They show reported activity, not a packet capture. Provider arrows show which backend an agent uses, not a verified network destination.

The dashboard can now finish first-run setup itself. Open it for a project folder and use the **Set up agents** card to create `.agentctl/agents.json`; existing guidance and definitions are preserved. The managed team is drawn on the map and listed under **Details**. Configure its roles/tools/checks, then select Managed feedback in the launch form. These workflows automatically pass structured review feedback into the next iteration. CLI-launched managed runs also appear for monitoring and addressed operator messages. Existing workflow templates below remain fixed job graphs. See [Portable agent loops](AGENT_LOOPS.md) for setup and exact behavior.

Version 0.3 extends the existing agentctl project with a local browser GUI. The underlying tools remain your installed Codex/OpenCode CLIs and optional Ollama server. There is no additional agent framework, hosted account or JavaScript build step to maintain.

## Start

Requires Python 3.11+ and Linux. Windows users should run these commands, agentctl and the agent CLIs inside WSL.

From the extracted project directory:

~~~bash
# Offline interface demo: temporary project and clearly labeled synthetic events.
python3 -m agentctl dashboard --demo

# Your actual project; stop the demo first with Ctrl+C.
python3 -m agentctl -C /path/to/qte init
python3 -m agentctl -C /path/to/qte dashboard
~~~

The default URL is http://127.0.0.1:8765. Change the port with --port 8766. Use --no-browser to print the URL without trying to open it. The server binds only to IPv4 loopback.

Alternatively, run bash install.sh --replace, then use agentctl instead of python3 -m agentctl. The self-contained executable includes all HTML/CSS/JS; it works when launched from a different directory.

## Connect your CLIs

Log in to the CLI in your normal terminal first. In **Connections**, enter an executable name on PATH or an absolute executable path. Arguments and shell fragments do not belong in this field. The dashboard inherits the server process environment and reuses CLI authentication. It has no API-key entry form.

**Save** stores the executable override for dashboard jobs. **Check CLI** invokes --version; a successful check confirms executable availability, not provider authentication or account quota.

The GUI supports Codex, OpenCode, a direct read-only Ollama analyst, and the existing experimental Codex/Ollama route. Direct local analysis has no tools.

### Access and scheduling

Codex runs explicitly select a read-only or workspace-write sandbox. OpenCode uses the permissions in its own configuration. It is always scheduled as an exclusive potential writer, including when a workflow role asks it only to read. agentctl does not label OpenCode as having an OS read-only sandbox.

The OpenCode adapter runs its documented JSON CLI with the packet attached as a file. It probes run --help and requests a private standalone server when the installed CLI supports it. It does not pass --auto or a permission-bypass flag. Requests requiring interactive approval can fail or time out. Use the CLI's own terminal UI for permission conversations.

Up to max_workers Codex readers can overlap. A writer waits for all managed readers to finish and runs alone. Existing operation locks coordinate with ordinary agentctl terminal commands. This is one shared project checkout, not separate Git worktrees. The GUI has one controller per project.

## Work with agents

1. Click **New agent** or press N.
2. Select a single task or a built-in workflow.
3. Supply a name, concrete goal, CLI, optional model and relevant relative input files.
4. Launch. The job queues immediately, and its context packet is prepared against current source when it actually starts.
5. Select the agent to inspect reported messages, tool calls and CLI diagnostics.
6. Use **Stop** to cancel a queued task or terminate a managed process group.
7. After the task ends, send a follow-up or choose **Run again**.

Follow-ups start fresh processes and carry the previous reported result with current source. They do not replay the complete conversation or inject keystrokes into an existing terminal. CLI-native session IDs remain visible for manual inspection.

Workflow dependencies pass bounded reported results and artifact references to later roles. A dependency's successful execution unblocks its next job; it does not independently establish correctness. A failed or cancelled dependency blocks dependent jobs.

Closing a browser tab leaves the local server running. Ctrl+C in the server terminal cancels managed jobs and verification processes. After a crash/restart, unfinished records become interrupted; the app requires an explicit new run instead of automatically repeating actions.

## What the connection display means

| Display | Evidence |
|---|---|
| Working directory and CLI executable | Actual launch arguments and current project. |
| Process ID and native session ID | Managed subprocess and provider-reported event fields. |
| Provider, model, configured endpoint | Selected model and allowlisted fields in readable configuration. |
| Configured MCP integration | Name, public URL or executable in readable configuration; not a successful connection test. |
| Observed TCP connection | A live Linux socket associated with the managed process or discoverable descendants. |
| Tool activity | Structured events emitted by the selected CLI. |

Configuration hints are not a full reproduction of each CLI's resolver. Profiles, managed settings, JSONC, environment substitutions, hooks, plugins and shared services can add or override configuration. A missing endpoint is displayed as unresolved, never guessed. URLs omit user information, query strings and fragments. Live sockets show remote IP/port; proxies can obscure the actual upstream provider, and short connections may be missed between samples. Provider completion and emitted usage are not inferred from socket activity.

The wrapper's context estimate remains heuristic. In particular, its startup guidance estimate does not reproduce OpenCode's configuration or instruction discovery. Read the packet and use provider-reported usage when available.

## Results and verification

**Results** shows the agent's final reported answer, observed scanned-file changes, recorded usage, artifacts, and historical explicit checks. Open PACKET.md to inspect the actual input and answer.md for the saved output. Raw logs stay in the project's state directory; browser previews use best-effort redaction and are bounded.

When the queue is idle, **Run check** executes a command you provide and records its exit status and source fingerprint. For example:

~~~text
python3 -m unittest discover -s tests
~~~

Commands are split into argument arrays. Shell operators, substitutions and pipelines are not evaluated. Run a trusted script if a check needs several shell steps. Historical checks can become stale after source changes; agentctl status RUN_ID reports freshness against scanned source.

## Backtesting research: inputs and outputs

Select **backtest-research** to reuse your existing engine through three role contracts:

| Agent | Input | Output |
|---|---|---|
| experiment-design | Existing strategy interface, data schema, fixed evaluation protocol, objective and compute budget. | One hypothesis, parameter ranges, proposed experiment specification and explicit missing inputs. |
| experiment-run | The design and documented backtest/optimizer entry point. | Actual run artifacts, every attempted trial, commands, code/data versions, seeds and measured results. |
| experiment-review | Specification, trial history, metrics and underlying artifacts. | Evidence-based shortlist or a report that no candidate qualifies. |

The runner does not bundle QTE, market data, a strategy optimizer or a broker integration. An agent can operate your actual backtest CLI once its entry point and required protocol are supplied. If those are missing, the contract requires reporting the blocker. Numerical calculations belong in the engine/optimizer; the agent reviews outputs at experiment boundaries.

Include your framework's README/API documentation and a protocol file when launching. For example, your project could provide this **illustrative** development protocol. Values are examples to replace with choices appropriate to your data and objective:

~~~json
{
  "protocol_id": "research_v1",
  "data_id": "your_versioned_dataset",
  "strategy_id": "an_existing_registered_strategy",
  "split_manifest": "config/development_splits.json",
  "cost_model_id": "your_fixed_fee_spread_slippage_model",
  "benchmark_id": "your_fixed_benchmark",
  "objective": "median_net_validation_sharpe",
  "constraints": {"max_drawdown": 0.20},
  "max_trials_total": 100,
  "max_wall_seconds": 1800,
  "holdout_available_to_research": false
}
~~~

Ask the experiment to produce a summary with this shape, filled by actual code rather than invented by the model:

~~~json
{
  "experiment_id": "assigned_by_your_runner",
  "protocol_id": "research_v1",
  "strategy_version": "source_hash_or_commit",
  "data_version": "dataset_hash_or_version",
  "status": "completed_or_blocked_or_failed",
  "trials_attempted": null,
  "selected_parameters": null,
  "validation_metrics": null,
  "benchmark_metrics": null,
  "robustness_checks": [],
  "artifact_paths": [],
  "unresolved_issues": []
}
~~~

This template writes only beneath a unique deliverables/backtest-research directory, audited against scanned source. It preserves the evaluator, scoring, data, splits and cost model. Commands must support keeping their outputs there; otherwise the agent should report the integration gap. It excludes final holdout evaluation from the feedback loop. A prompt is not a substitute for access controls if the research agent must be unable to read a sealed dataset.

For optimization and experiment tracking, existing projects to integrate at the framework layer include [Optuna](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/009_ask_and_tell.html) and [MLflow](https://mlflow.org/docs/latest/ml/tracking/). Repeated selection from many backtests introduces selection bias; see the primary work on [backtest overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) and the [Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).

## Local state and HTTP boundary

The dashboard reuses the existing SQLite run store and artifacts. Its small additions inside the project's state directory are dashboard-settings.json, dashboard-jobs.json and dashboard.lock. No schema migration is required for existing runs. Older terminal runs appear in **History**.

The HTTP API requires a random session token, checks Host/Origin, serves no cross-origin API, and confines artifact previews to a fixed allowlist. The dashboard is a trusted local application capable of launching your CLIs and explicit checks. Do not expose it as a public service.

## Validation limits

Automated tests use local fixture CLIs and cover the subprocess, queue, HTTP, artifact and packaged-resource paths. A Firefox browser check with synthetic activity exercised animated and resting portraits, keyboard selection, inbound messages, the addressed composer and reduced motion. It also exercised selection on a narrow screen with 13 agents. These checks used offline fixtures throughout.
