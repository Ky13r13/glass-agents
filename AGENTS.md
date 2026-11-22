# Working on agentctl

The core is a Python 3.11+ standard-library Linux CLI; managed agents use optional Claude Agent SDK/jsonschema dependencies. Keep scan/prepare/validation paths free of model calls and project-script execution. Preserve existing project instructions. Never label provider completion or reviewer acceptance as verified correctness. Keep estimates distinct from actual reported usage and unknown cost.

The runner uses argument arrays and subprocess groups. Maintain timeouts, both-pipe draining, source freshness checks, and explicit sandbox settings. Writers must remain exclusive until a real worktree integration mechanism exists.

Relevant files: `agentctl/project.py` for repository evidence; `context.py` for packets; `runner.py` for execution and evidence; `team.py` for scheduling; `local.py` for Ollama; `cli.py` for commands.

For behavioral changes, run `python3 -m unittest discover -s tests -v`. Update the README and command reference when CLI behavior changes. Tests use local fixtures; do not represent them as actual GPU or hosted-provider benchmarks. No credentials or model weights belong in this repository.
