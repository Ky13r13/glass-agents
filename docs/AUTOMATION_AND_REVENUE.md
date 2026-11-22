# Turn workflows into a paid service

Sell a useful, checked outcome. Start with one repeatable problem and one paying customer; measure delivery effort before offering a recurring service. The workflows prepare local drafts. They do not find customers, connect their accounts, invoice them, or deliver reports automatically.

## Good starting offers

| Offer | Inputs and outcome | Workflow | Human acceptance |
|---|---|---|---|
| Weekly operations report | Authorized CSV exports → reconciled metrics and exception report | `client-report` | Check reporting period, totals, definitions, and exceptions against source data |
| BOM cleanup | Parts list → duplicate, revision, quantity, and unit discrepancies | `bom-audit` | Confirm findings with the responsible engineer; never treat inferred substitutions as approved |
| Technical documentation | Existing code and supplied procedures → updated setup guide or SOP | `docs` | Test instructions and have the process owner approve them |
| Small software maintenance | Reproducible defect → patch, tests, review | `bugfix`, `tests`, `code-review` | Run relevant checks and approve delivery/deployment |
| Project scoping | Customer requirements and your supplied rates → draft scope and estimate | `quote-draft` | Resolve assumptions and approve price and commitments |

A practical first offer is: “I turn your weekly export into a checked report showing missing records, duplicates, and exceptions.” Choose a domain you already understand. Use synthetic or authorized sample data to demonstrate it.

The SBA recommends researching demand, competition, and customer needs before committing to a business: https://www.sba.gov/business-guide/plan-your-business/market-research-competitive-analysis . Upwork's 2026 skills report lists scripting/automation and data analytics among in-demand skills, but its marketplace evidence does not establish your expected income: https://www.upwork.com/press/releases/upworks-in-demand-skills-2026-demand-for-top-ai-skills-more-than-doubles-as-ai-is-embedded-into-everyday-work .

## From pilot to recurring work

1. Ask potential customers what repetitive report or cleanup consumes time, how often it occurs, and what a correct result looks like.
2. Agree on one bounded paid pilot: specified inputs, output, deadline, acceptance checks, and revision allowance. Obtain permission for the data and processing tools used.
3. Run the workflow manually. Record time spent gathering inputs, checking output, revising, and supporting the customer.
4. Turn recurring arithmetic and validation into a tested Python or SQL pipeline. Use agents to explain verified results and investigate exceptions. Do not ask a language model to be the source of truth for totals.
5. Repeat successfully before scheduling. Offer recurring service only when scope and effort are predictable. Keep delivery under review until its own acceptance checks and authorization are implemented.

Use one project per client. Put a brief in `inputs/brief.md` containing the reporting period, filenames, column meanings, metric formulas, required output, and acceptance checks. Store only data you are authorized to use. Large datasets should be read by a deterministic script or Codex tools; bounded context excerpts are not a complete dataset.

```bash
cd /path/to/client-project
agentctl init
agentctl workflow run client-report \
  --goal "Use inputs/brief.md; reconcile source totals, identify exceptions, and draft the weekly report" \
  --include inputs/brief.md --dry-run
# Inspect the plan, then repeat without --dry-run.
```

For a robust recurring report, build these gates into its data script: required inputs exist; expected period matches; schema and units match; row counts and totals reconcile; validation failures stop the report. Missing input must produce a failure, not an invented result. These client-specific validators and data connectors are not bundled with agentctl.

## Pricing by observed effort

Example arithmetic only, not suggested market rates or an earnings forecast:

- Monthly customer fee: $300.
- Direct tools/model costs: $25.
- Your review and support: 2 hours valued at $50/hour = $100.
- Remaining contribution: $175 before acquisition, setup, administration, taxes, and other expenses.

Charge separately for setup when appropriate, and price from measured effort and customer value. Track every model run and revision. Context budgets bound prepared excerpts, not total provider spend. Timeouts and worker limits are not dollar caps; tool loops can add usage.

## Optional Linux scheduling

The supplied user-level systemd examples prepare a draft every Monday at 08:00 in the machine's local timezone. They are examples only; nothing is enabled by installing agentctl. First complete a successful manual run using your installed Codex version and account. Unattended runs cannot answer approval prompts.

1. Edit `examples/agentctl-weekly-report.service`: replace `%h/client-project` with your project path in both places. The example assumes agentctl is installed at `~/.local/bin/agentctl`.
2. Set `codex_bin` in the project's `.agentctl/config.toml` to the absolute Codex executable path if it is not on the service's PATH. Keep authentication available to the same Linux user.
3. Keep the brief and inputs current. The template has no download step; arrange an authorized input export separately.
4. From the extracted source directory, install and test the unit:

```bash
mkdir -p "$HOME/.config/systemd/user"
cp examples/agentctl-weekly-report.service examples/agentctl-weekly-report.timer \
  "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user start agentctl-weekly-report.service
journalctl --user -u agentctl-weekly-report.service -n 100 --no-pager
```

Inspect the draft and individual review answers in the project's `.agentctl/runs/` directories. A successful service exit only means execution completed; reviewers can still report `NEEDS_WORK`. There is no automatic semantic acceptance gate.

Once satisfied, enable the timer:

```bash
systemctl --user enable --now agentctl-weekly-report.timer
systemctl --user list-timers
# To stop future scheduled starts:
systemctl --user disable --now agentctl-weekly-report.timer
```

The computer must be running and its user service manager active at the scheduled time. A user timer is not a guarantee of execution while logged out; continuous user services require additional host configuration. `Persistent=false` avoids catch-up runs for missed calendar times. Stopping the timer does not cancel an already running service.

Each report uses three agent jobs (audit, draft, review), and every job can involve multiple model calls. Test one customer first, inspect actual usage, and avoid overlapping GPU jobs on your 16 GB machine. Local document readers are optional; numeric analysis and final review stay on Codex in the supplied workflows.

## What to automate next

After a paid pilot, build the customer's exact input validator and report generator first. Then add an authorized input connector, failure notifications, and an explicit approval record. Client delivery, billing, and production deployment need separate integrations and authorization. agentctl currently stops at local work prepared for your review.
