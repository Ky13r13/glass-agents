# Commands and configuration

Global directory option: `agentctl -C /path/to/project COMMAND`. The directory flag goes before the subcommand. The app finds the nearest initialized ancestor, otherwise the Git root, otherwise the supplied directory.

| Command | Action |
|---|---|
| `agents init` | Attach portable managed agent definitions to the exact selected project directory. |
| `agents list` / `agents validate` | Inspect roles, tools, workflow limits and optional runtime availability without launching agents. |
| `agents run GOAL [--workflow default] [--config PATH] [--prepare-only]` | Run the selected workflow: worker/check/reviewer feedback, or a swarm. |
| `agents run GOAL --workflow SWARM [--replicas N] [--rounds N]` | Run parallel candidates in isolated Git worktrees; override copies per candidate (1–10) or maximum rounds (1–20). |
| `agents ask NAME TASK [--in FOLDER] [--scope GLOB] [--read-only] [--check TOOL] [--prepare-only]` | Give one named agent one task. `--in`, `--scope` and `--read-only` can only narrow its configured write scope. No reviewer, no revision. |
| `agents apply ID [--candidate SLOT] [--allow-unaccepted] [--no-checks]` | Apply a finished swarm's winner (or chosen candidate) to the working tree and rerun required checks there. |
| `agents demo [--output NEW_DIR] [--swarm]` | Run isolated scripted agents with real tools and check-driven feedback; `--swarm` uses real worktrees. No model calls. |
| `agents message ID --to NAME --text TEXT` | Queue operator feedback for a managed agent. |
| `agents messages ID [--to NAME]` | Read addressed operator and peer messages in a managed run. |
| `agents events ID [--follow]` | Inspect the actual event stream. |
| `dashboard [--port 8765] [--no-browser]` | Open the local browser dashboard. Use Teams to draft agents from a prompt, save project-local definitions, and launch a workflow. |
| `dashboard --demo` | Preview isolated synthetic CLI activity without connecting a model. |
| `init [path]` | Create minimal helpers and a fresh repository map; preserve existing guidance/config. |
| `inspect [--json]` | Read-only inventory, helper recommendations and inferred commands. |
| `doctor` | OS, RAM, GPU/driver, executable and Ollama checks. |
| `context GOAL` | Prepare a packet without a model call. |
| `start GOAL` | Prepare and execute a scripted task. |
| `start GOAL --interactive` | Open Codex's terminal UI with the packet. |
| `run [ID]` | Execute a prepared, unchanged packet. Latest run if ID omitted. |
| `status [ID] [--json]` | List runs or inspect one run and its current evidence. |
| `checkpoint --note TEXT --next TEXT` | Save current source state and next action. |
| `resume [FOLLOWUP]` | Build a fresh continuation from the saved handoff. |
| `verify ID -- COMMAND ARG...` | Run a check directly, without a shell; persist evidence. |
| `plan GOAL [--local-readers] [--output PATH]` | Save a proposed graph without model calls. |
| `team PLAN.json [--dry-run]` | Validate or execute the graph. |
| `models list` | List installed models on the configured server. |
| `models status` | Show loaded models and placement metadata. |
| `models serve` | Start an Ollama foreground server with conservative defaults. |
| `models setup [--base NAME] [--model ALIAS]` | Download a base model and create a tuned alias. |
| `models benchmark [--model NAME]` | Run and save a small structured analysis check. |

`start`, `context`, and `resume` accept `--backend codex|opencode|local|codex-local|claude-sdk`, `--include PATH` (repeatable), `--budget N`, `--model NAME`, `--read-only`, `--prepare-only`, and `--interactive`. OpenCode requires workspace-write scheduling; --read-only is not supported for that backend. Managed feedback uses exclusive scheduling and per-agent scopes from agents.json; use `--agent-workflow` or `--agents-config` to choose alternate definitions. Only hosted Codex supports interactive terminal mode. `resume --run ID` selects a particular earlier run. `checkpoint --run ID` selects the run to describe.

`agents run` and `agents ask` route messages and delegated tasks using each agent's `peers` list across all managed backends. `agents events ID --follow` shows calls and returned results. In `examples/agents-mixed.json`, the `codex-led` workflow starts a Codex coder with a Claude SDK advisor and reviewer.

`codex-local` is experimental and read-only. The `local` backend is a read-only analyst. Within a managed workflow it can request actions from Glass through structured responses. Select a new `start` task to move from an earlier read-only continuation to an editing task.

## Editable defaults

**dashboard** supports --port, --no-browser and --demo on Linux, WSL, and native Windows. Put the global -C project option before the command. The dashboard initializes the selected project when needed. The synthetic demo creates its own temporary workspace. **Teams** can draft with Codex, Claude Code, or Ollama, shows draft stages and elapsed time, and allows cancelling an in-progress draft.

The scripted start/context/resume commands also accept **--backend opencode**. OpenCode runs use the CLI's own permissions and require workspace-write scheduling; --read-only is rejected for this backend. Interactive terminal mode remains Codex-only. The dashboard supports managed headless tasks and fresh follow-ups.

The new **opencode_bin** config key defaults to "opencode". GUI executable overrides are saved separately in the project's dashboard-settings.json inside its state directory. They apply to dashboard jobs; terminal commands continue to use config.toml.

Edit `.agentctl/config.toml` in the project. Unknown keys are rejected to expose typos.

| Key | Default | Meaning |
|---|---|---|
| `context_budget` | 6000 | Estimated supplied prompt + observed guidance for hosted runs. |
| `local_context_budget` | 2500 | Estimated starting packet allowance for local routes. |
| `max_file_bytes` | 98304 | Maximum source file size to read. |
| `max_files` | 5000 | Maximum eligible text files in an inventory. |
| `max_scan_bytes` | 25000000 | Maximum eligible text bytes read per inventory. |
| `max_workers` | 2 | Maximum simultaneous team readers; writers remain exclusive. |
| `timeout_seconds` | 1800 | Scripted task/request/check deadline. Interactive sessions have no wrapper deadline. |
| `codex_bin` | `codex` | Executable name or absolute path. |
| `opencode_bin` | `opencode` | OpenCode executable name or absolute path. |
| `ollama_url` | `http://127.0.0.1:11434` | Loopback HTTP endpoint. |
| `local_model` | `agentctl-helper:latest` | Local model alias used for analysis. |
| `local_base_model` | `qwen2.5-coder:7b-instruct-q4_K_M` | Explicit model to download during setup. |
| `local_num_ctx` | 8192 | Local context requested from Ollama. |
| `local_num_predict` | 1024 | Direct local response token allowance. |

The local packet allowance + output allowance + 512 must fit the configured local context. This check uses a heuristic estimate and does not guarantee model-token fit. A packet budget is not a total-run token cap: later Codex tool calls and conversation turns add tokens. Actual hosted account limits are managed by the provider.

## Job graph format

See `examples/team.json` for an executable shape. Each job requires a unique ID and a nonempty goal. Optional fields are backend, permission, includes, model, depends_on, and write_scope. The graph must be acyclic and all dependencies must exist.

`write_scope` accepts relative paths, directory prefixes, or fnmatch-style globs. The default is the whole repository. Scopes are audited against scanned text files; ignored/oversized/binary files are not covered. An empty list currently uses the default whole-repository audit scope, so use `permission: read-only` for readers rather than treating an empty scope as a sandbox.

A dependency's successful execution allows the next job to start; it does not certify the dependency's reasoning. Ask the final worker to inspect evidence and verify assumptions.

## Exit codes and failure records

Normal success is exit 0. Managed runs return 0 only when the loop outcome is `accepted` (feedback or swarm) or `completed` (an `agents ask` task whose required checks passed); `checks_failed`, `rejected`, `limit_reached`, `failed` and `cancelled` return 1. `agents apply` returns 1 when integration checks fail. A failed scripted run or team returns 1; invalid arguments/configuration or a missing runtime usually returns 2. `verify` returns a conventional nonzero check code when possible. An interrupted top-level command returns 130, though an interrupted active run can return 1 after persisting its cancellation state.

No automatic retries occur. Merges happen only through a swarm's explicit integration step (`apply_winner` or `agents apply`), which refuses to run if scanned project files changed since the swarm started. After a crash, a record can remain `running`; inspect its artifacts and start a fresh continuation. Locks are released by the OS when the holding process exits. Do not delete the SQLite file to clear a lock.

## Official interfaces used

- [AGENTS.md discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Codex non-interactive output and usage](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex CLI commands and local provider flags](https://learn.chatgpt.com/docs/developer-commands)
- [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Ollama chat API](https://docs.ollama.com/api/chat)
- [Ollama model listing](https://docs.ollama.com/api/tags)
- [Ollama loaded model listing](https://docs.ollama.com/api/ps)

Interfaces were checked on 2026-10-01. The package records installed versions through `doctor`; compatibility should be rechecked when updating either runtime.
