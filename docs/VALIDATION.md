# Validation record

Version: 0.1.0. Checked 2026-10-01 in a Linux environment with Python 3.12.14 and Git 2.51.1.

Command:

```bash
python3 -m unittest discover -s tests -v
```

Result: **28 tests passed** in the final run (approximately 5.9 seconds).

## What was exercised

| Area | Evidence |
|---|---|
| Initialization | Repeat initialization preserves existing guidance; root overrides prevent duplicate guidance creation. |
| Scanning | Git ignore rules, common secret paths, generated directories, symlinks and explicit include containment. |
| Context | Selected source references, redaction, budget accounting, nested instructions, refusal to silently truncate required guidance. |
| Freshness | Prepared runs reject changed source and changed global guidance. |
| Process runner | Real subprocesses emit simulated Codex events; both output pipes are drained; deadline termination is exercised. |
| Failure semantics | A reported failed turn is a failure even with process exit zero. |
| Scope | Unexpected writes are detected for read-only and restricted-scope jobs. |
| Verification | Commands actually run; success, nonzero exit and stale evidence are distinguished. |
| Continuation | Fresh source changes are surfaced; repeated resumes preserve the original objective, read-only permission and selected model. |
| Concurrency | Two readers actually overlap, the writer starts after them, and a failed dependency blocks its writer. |
| Storage | An operation lock blocks concurrent operations; symlinked state locations are rejected. |
| Local API | A loopback HTTP fixture receives bounded context settings and no tools; usage and measured-rate calculations are exercised. |
| Packaging | The installer builds the executable zipapp; that executable launches a local worker from a different directory against the HTTP fixture. |

## Limits of these results

Hosted Codex and an actual Ollama model were not available in the test environment. The test provider processes and HTTP server are fixtures; these tests do not establish compatibility with every runtime release, real model quality, account permissions, GPU speed, or actual VRAM fit.

The 1080 Ti profile is a recommended starting configuration based on documented runtime support and model size. Run `agentctl doctor`, `agentctl models setup`, `agentctl models benchmark`, and `ollama ps` on the target PC before relying on local performance. Start with a read-only task to check your actual Codex installation.

There is no claim of measured token savings. Compare representative completed tasks, including retries and verification work, before deciding whether local helpers or parallel readers improve your workflow.

## Version 0.2.0 workflow extension

All 36 automated tests passed on 2026-10-07. Eight new tests cover workflow graph validity, read-only roles, unique scoped deliverables, unsafe output paths, local routing, dry-run non-execution, CSV/TSV selection, and scheduler execution with a fixture provider. Existing lifecycle and installed-zipapp tests also pass. Live Codex, GPU performance, customer outcomes, and scheduled host execution were not tested here.

## Version 0.3.0 dashboard extension

Checked on 2026-11-21. The final suite contains **55 automated tests**, covering the original behavior plus:

- Codex/OpenCode subprocess fixtures, completion/failure handling, usage and command recording.
- Queue dependencies, fresh source preparation after a writer, reader overlap, exclusive writers, dependency blocking and operation locks.
- Active and queued cancellation, no launch after pre-cancellation, follow-up context and interrupted-record recovery.
- Exclusive dashboard ownership so a second server cannot rewrite an active controller's records.
- Loopback HTTP access, session tokens, Host/Origin checks, artifact containment and incremental event reads.
- Provider/endpoint metadata excluding authentication fields, live owned-socket observation and cancellation of explicit verification.
- A built, installed zipapp serving its actual HTML/CSS/JavaScript from another working directory.
- The backtest research role template's scope and preservation of evaluation protocol.

Commands:

~~~bash
python3 -m unittest discover -s tests -v
node --check agentctl/web/app.js
~~~

The JavaScript syntax check is a development check; Node is not an application runtime requirement.

The installed Codex executable's help was inspected. Authenticated provider sessions, actual OpenCode installation, GPU/model performance and real QTE backtests were not exercised. CLI fixtures are not provider benchmarks. The cloud browser could not access the local preview, so no successful visual or interactive browser test is claimed.

## Version 0.4.0 managed agents

Checked on 2026-11-21 with Claude Agent SDK 0.2.187 installed in an isolated dependency directory. The suite has **82 tests**. The optional real-SDK smoke test is skipped when the dependency is absent and is exercised when installed.

New coverage includes:

- Portable/idempotent project initialization, typed command configuration and malformed schemas/peers/scopes.
- A complete worker/advisor/check/reviewer cycle, failed check, feedback message, revision and passing check.
- Machine check failure overriding reviewer acceptance; rejection and iteration/call/tool/depth limits.
- Literal argument substitution, JSON stdin envelopes, fixed context, output schema errors, timeouts and cancellation.
- Read-only/path/symlink/ignored-file enforcement, source-integrity gates and mutated frozen configuration.
- Fresh source context for every managed call, including reviewers inspecting the worker's latest changes.
- Operator messages, CLI events, terminal-launched dashboard monitoring, native Codex peer handoffs and installed-zipapp execution.
- SDK custom-tool registration, explicit tool/MCP configuration, internal StructuredOutput handling, error results, usage/cost recording and exclusion of hidden reasoning from events.
- Concurrent first-time SQLite initialization, including a reproduced startup WAL race now serialized.

The actual SDK API was used for options and MCP server/tool construction; an offline client fixture exercised the bridge without calling a model. Scripted demos use real command/file evidence with synthetic agent decisions and are labeled accordingly. Authenticated live SDK/Codex/OpenCode sessions, real project-specific backtests, financial outcomes and visual browser operation were not tested.
