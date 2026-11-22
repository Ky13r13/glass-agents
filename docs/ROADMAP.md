# Scope and development roadmap

## Implemented in 0.1.0

- Dependency-free Linux CLI and single-file zipapp installer.
- Repository scanning with Git ignore handling, exclusions and source hashes.
- Existing-guidance preservation and minimal helper creation.
- Explanation of which additional Markdown documents would help.
- Task-relevant excerpts, explicit includes, instruction estimates and manifests.
- Hosted Codex scripted and interactive launch paths.
- Scripted JSON event capture, usage, time/output limits, process cancellation and failure detection.
- SQLite run records, explicit check evidence, stale-check detection, handoff and compact continuation.
- Deterministic task-graph recommendation and graph validation.
- Bounded parallel readers, exclusive writers and serialized local requests.
- Ollama diagnostics, foreground serving, model download/alias setup, measured smoke benchmark and read-only analysis.
- Experimental read-only Codex/local-model route with an advertised-tool-support check.

## Implemented in 0.2.0–0.4.0

- Reusable workflow role templates and editable task graphs.
- Local dashboard with Codex/OpenCode launch, activity, connection metadata, cancellation and check evidence.
- Portable named agent definitions and optional Claude Agent SDK integration.
- Bounded worker/check/reviewer feedback loops, scoped custom tools and managed peer delegation.
- Agent mailboxes, typed project-command inputs/outputs and dashboard monitoring of CLI-launched managed runs.
- Fresh source context for each managed call and current check evidence required for gated acceptance.

## Next work, in order

1. Run representative tasks on the actual 1080 Ti and record latency, memory placement, answer quality and retry rate. Compare local-helper runs with hosted-only runs before enabling them routinely.
2. Improve retrieval only where measured misses occur: symbol extraction and import/dependency relationships before adding an embedding service.
3. Add optional structured LLM planning when the simple component heuristic fails to form useful tasks. Keep graph validation and explicit execution separate.
4. Expand recovery beyond local run records and add richer native Codex session integration if long unattended runs are needed.
5. Implement separate Git worktrees, conflict detection and final integration verification before allowing concurrent writers.
6. Add remote worker deployment and authenticated remote model hosts only when there is an actual additional machine to target.

Not included: automatic model-quality certification, a hard hosted-token spending cap, driver installation, remote deployment, automatic merging, vector databases, fine-tuning, or guaranteed autonomous local coding.

## Why this version is small

Ordinary code performs scanning, hashing, filtering, state storage, and scheduling. Codex supplies the coding runtime. The local model is optional and bounded. This keeps maintenance and background memory use low while making the workflow inspectable.

Track total provider-reported tokens, wall time, retries, and verification outcomes per task. A smaller prompt or more workers is only an improvement if the resulting task is completed correctly with better overall resource use.
