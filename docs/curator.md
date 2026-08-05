# Gnojo Curator

Gnojo Curator is a persistent Knowledge Operations Engine. It inventories workflows, articles, commands, and scripts; reuses Gnojo's existing validators; remembers observations across audits; maintains Knowledge Tasks; measures Knowledge Debt and Knowledge Health; and learns evidence-backed patterns without changing trusted content.

## Architecture

Curator uses small, independently testable services:

- `CuratorAuditor` performs deterministic inventory and checks.
- `CuratorMemoryStore` atomically persists operational memory in `curation_memory/memory.json`.
- `KnowledgeTaskService` reconciles stable findings into non-duplicating Knowledge Tasks.
- `KnowledgeDebtService` scores active work using classification, priority, recurrence, and age.
- `KnowledgeHealthService` publishes bounded content, workflow, relationship, source, coverage, and validation indicators.
- `CuratorLearningService` records recurring evidence patterns but never changes policy or content.
- `KnowledgeOperationsService` orchestrates the services and creates an immutable per-run snapshot.
- `AuditReportWriter` writes human-readable and machine-readable reports.

Trusted content and operational memory are separate. Memory records audit history, task state, ownership, decisions, trends, and lessons; it is never treated as published knowledge.

## Finding model

Every observation has a classification and an independent severity:

- **Defect:** an objectively provable schema, relationship, source, parser, metadata, reachability, or validation failure.
- **Risk:** valid content that may benefit from human review for safety, clarity, consistency, rollback, prerequisites, or lifecycle alignment. A risk does not claim that content is wrong.
- **Opportunity:** a non-problematic editorial suggestion for reuse, supporting knowledge, command references, shared logic, convergence, or simplification.
- **Recommendation:** a higher-level improvement to taxonomy, standards, coverage, authoring, reviewing, reporting, or Gnojo itself.

Draft, pending-review, reviewed, published, and archived records are separate lifecycle states. Duplicate detection compares records only inside the same lifecycle, so an editable draft and its published source are not duplicates.

## Graded safety model

| Level | Expected guidance | Representative actions |
| --- | --- | --- |
| 0 | none required | open Settings, inspect Device Manager |
| 1 | brief reminder or impact note | restart an application, printer, or service |
| 2 | save-work or interruption guidance | restart Windows, networking equipment, router, or modem |
| 3 | backup, restore, rollback, or recovery preparation | registry edits, System Restore, driver rollback |
| 4 | authorization, backup, power, and recovery guidance | firmware/BIOS updates, partitioning, Reset Windows |

Safety findings are proportional review risks, not automatic claims that an instruction is unsafe.

## Run an audit

From Gnojo, open **Content Studio → Curator Dashboard**, then select **Run Curator Audit**. The dashboard displays the latest health and debt indicators, persistent Knowledge Tasks, lessons, and recent audit history.

The same operation is available from the repository root:

From the repository root:

```powershell
python -m curator audit
python -m curator audit --platform Windows --content-type workflow
python -m curator audit --category Networking --severity high
python -m curator tasks list
python -m curator tasks update GKT-123456789ABC --status in_progress --owner "QA Reviewer" --note "Validated reproduction."
```

`--changed-since` is reserved for a later milestone and is recorded but not yet used to filter results.

Exit codes: `0` means no high/critical defects, `1` means high/critical defects require review, `2` means the audit failed, and `3` means another audit owns the lock. Risks, opportunities, and recommendations alone do not make the audit fail.

## Outputs

Each run creates a separate folder under `curation_runs/` containing the complete result, inventory, coverage report, classification files, domain-specific findings, a Markdown summary, and a JSONL event log. It also writes snapshots named `knowledge_tasks.json`, `knowledge_debt.json`, `knowledge_health.json`, `lessons_learned.json`, and `memory_summary.json`. Finding and Knowledge Task IDs remain stable for the same observation, while each execution receives a unique run ID.

## Persistent task lifecycle

Knowledge Tasks use `open`, `in_progress`, `resolved`, `ignored`, or `superseded`. A recurring finding updates its existing task. A previously resolved finding that reappears returns to `open` with history intact. Only a complete unfiltered audit may resolve an unseen task; filtered audits cannot infer that out-of-scope content is healthy.

Task changes made through the CLI record the actor, prior state, new state, timestamp, and note. Curator can propose and track work, but publishing, safety judgments, editorial wording, policy, and destructive actions remain human-gated.

## Knowledge Debt and Health

Debt is explainable rather than predictive: classification and priority establish the base score, while recurrence and age add bounded weight. Reports show total debt, trend, breakdowns, largest contributors, oldest work, and frequently recurring work.

Health scores are bounded indicators, not claims of correctness. The overall score averages six dimensions: content quality, workflow integrity, relationship health, source health, coverage health, and validation health. Each dimension subtracts the observed severity weight as a percentage of the audited inventory's maximum deterministic penalty. Every score includes its method and audit denominator.

## Memory ownership and migration

`curation_memory/` is runtime operational state and is ignored by Git by default. Back it up with the same retention controls used for audit artifacts. To move environments, copy the entire directory while no audit is running. Schema version changes must use an explicit migration; unknown versions fail closed rather than silently discarding history. Existing standalone reports remain immutable evidence. The first v0.3 audit establishes the persistent baseline; later audits compare against it.

The auditor checks metadata, generality, taxonomy, workflow integrity and reachability, content relationships and orphans, article/source evidence, command/script quality, graded safety, lifecycle-aware duplicates, reusable instruction patterns, command-reference candidates, workflow convergence, platform coverage, and known application rendering invariants.

The Markdown report separates Critical Defects, Content Risks, Knowledge Opportunities, Editorial Recommendations, Coverage Improvements, System Improvements, and Curator Suggestions.

## Trust boundary

Curator never silently modifies or publishes workflows, articles, commands, scripts, or approval state. Report output is rejected if it points inside a trusted content store. Findings include evidence, confidence, severity, and a recommended human action. Any future repair engine must remain a separate, explicit approval phase.

## Scheduling

Automation may invoke `python -m curator audit` on a schedule. The lock prevents overlapping runs. Scheduled jobs should retain the generated report directory and alert on exit code `1`, `2`, or `3`.

## Human review gates

Curator remains deterministic wherever possible. It may automatically reconcile task state, calculate metrics, and recognize repeated observations. It does not rewrite instructional content, alter workflow design, approve safety guidance, publish knowledge, or execute destructive repairs. Those decisions require a human reviewer.

## Future milestones

- approval-driven repair proposals with before/after diffs
- explicit execution plans and isolated test gates
- rollback metadata and provenance for approved changes
- live-link verification with bounded network policy
- automatic pull-request generation after human approval
