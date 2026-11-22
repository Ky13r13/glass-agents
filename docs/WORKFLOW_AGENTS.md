# Reusable workflow agents (0.2.0)

These workflows are role prompts plus validated dependency graphs, executed by the existing Codex/Ollama runner. They are bundled inside the executable, so they work after installation without external template files. They use the existing timeout, context, sandbox, evidence and concurrency settings.

```bash
agentctl workflow list
agentctl workflow run bugfix --goal "Fix CSV quoting; preserve existing output columns" --dry-run
agentctl workflow run bugfix --goal "Fix CSV quoting; preserve existing output columns"
```

The first command lists the catalog. `--dry-run` saves and prints a graph without launching models. Omitting it executes the workflow. Every stage is a model run and consumes its own context; use a single `start` task for tiny edits.

| Workflow | Sequence | Output |
|---|---|---|
| `bugfix` | Diagnose → implement → review | Focused fix and evidence-backed findings. |
| `code-review` | Review | Read-only actionable findings. |
| `tests` | Test plan → build tests → review | Regression/edge-case tests and their observed results. |
| `docs` | Audit docs → write docs → review | Documentation grounded in source. |
| `release-check` | Parallel test-plan + doc-audit → review | Readiness assessment; no deployment. |
| `client-report` | Audit data → write report → review | A local draft report with source and calculation references. |
| `bom-audit` | Audit BOM → write report → review | Part-table inconsistencies, units and revision exceptions. |
| `quote-draft` | Scope brief → draft estimate → review | Scope, exclusions, assumptions, and estimate arithmetic. |

The detailed role definitions live in `agentctl/workflows.py`. Add a role contract there, then assemble its dependencies in `WORKFLOWS`. Writers remain exclusive; independent readers can overlap within `max_workers`.

## Common commands

```bash
agentctl workflow run code-review --goal "Review the CSV import code for data loss and edge cases"
agentctl workflow run tests --goal "Add regression tests for malformed CSV rows" --write-scope 'tests/**'
agentctl workflow run docs --goal "Correct setup commands in README.md" --write-scope README.md
agentctl workflow run release-check --goal "Assess whether version 1.2 is ready for human release review"
```

The testing workflow assumes the requested behavior already exists or its expected behavior is specified. A read-only review may be unable to run tests that require writing caches/build artifacts. It must report that limitation; run the actual build/test command through `agentctl verify` as appropriate.

## Client-service workflows

First place an authorized client brief and input files in a dedicated project. The brief should state the reporting period, units/currencies, expected schema, the metric definitions, handling of missing records, and acceptance criteria. For quotes, supply your own rates, time estimates and cost assumptions.

```bash
agentctl init
agentctl workflow run client-report \
  --goal "Analyze inputs/production.csv for the period in inputs/brief.md. Report scrap rate and late orders; reconcile totals and flag missing records." \
  --include inputs/brief.md

agentctl workflow run bom-audit \
  --goal "Audit inputs/parts.csv for duplicate part IDs, missing revisions and inconsistent units. Preserve the original file." \
  --include inputs/brief.md

agentctl workflow run quote-draft \
  --goal "Draft a scope and estimate for the requested reporting automation using only the rates and assumptions in inputs/brief.md" \
  --include inputs/brief.md
```

Default client deliverables are placed in unique `deliverables/<workflow>/<run-id>/` directories. You can set `--output-dir deliverables/pilot-01`; when reusing a directory, the writer may update its existing files. Choose a new directory to preserve an earlier draft. The final reviewer returns its findings to the saved run answer; it does not edit the report.

Small CSV/TSV files can now be selected as context. Files over the normal scan size limit are not embedded, so include a short brief naming them and let hosted Codex inspect them with tools. Excerpts are never sufficient proof of full-dataset totals. Data/BOM audits use hosted Codex with tools; the local 7B helper is not assigned numerical validation.

Keep input and delivery schemas stable. After building a recurring service, put arithmetic and validation in deterministic Python/SQL, and use agents for explanation and exceptions. These templates are an orchestration starting point, not a prebuilt production reporting system.

## Export and customize

```bash
agentctl workflow export client-report --goal "Prepare the agreed weekly exception report"
# Edit the printed JSON path if needed, then:
agentctl team .agentctl/plans/PRINTED_PLAN_FILENAME.json --dry-run
agentctl team .agentctl/plans/PRINTED_PLAN_FILENAME.json
```

`--include` and `--write-scope` are repeatable. Scopes are post-run audits of scanned files, not independent filesystem sandboxes. A file created above the scan size limit or in an excluded directory is outside this audit's coverage. The Codex sandbox is still the execution boundary.

`--local-readers` routes only `doc-audit` to the local helper. That stage can comment on supplied excerpts but cannot inspect unavailable files. Numerical validation, diagnosis, and final review remain on Codex.

## Execution versus acceptance

All workflows require human review. The final reviewer returns `READY_FOR_HUMAN_REVIEW` or `NEEDS_WORK` as an advisory assessment. The scheduler's `successful` field reports successful process/turn completion, not a parsed approval verdict. A reviewer finding a serious bug can still be a successful execution. Read its answer and check the evidence before delivering anything.

Role prompts prohibit customer contact, publishing, deployment, purchases, charging and remote account changes. These are behavioral instructions, not a technical guarantee about every connected tool. The package has no automated customer-delivery or payment step. Keep external credentials/tools limited to the permissions the job actually needs.

For routine scheduling, see [AUTOMATION_AND_REVENUE.md](AUTOMATION_AND_REVENUE.md) and the optional systemd example files. Nothing is scheduled merely by installing this package.
