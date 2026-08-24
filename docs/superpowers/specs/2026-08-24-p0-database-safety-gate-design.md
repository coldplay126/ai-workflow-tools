# P0 database safety gate design

## Goal

Prevent database-affecting work from passing AWF as a small staging-only change. A detected database change must become high risk, use current production schema metadata, compare viable data/query/schema choices instead of defaulting to an index, and carry deterministic evidence through G1, G5, and G6.

This P0 change establishes the fail-closed boundary. General planning-option selection and richer warehouse/DuckDB orchestration remain follow-up work.

## P0 incidents addressed

The current workflow can classify a short concept such as a fan-log ordering change as `small`. That skips review, approval, and verify even when the production schema has no supporting index or production data distribution differs from staging. AWF currently has no production-schema command, no normalized DB evidence, and no gate condition that survives the normal risk-based skip policy.

The P0 risks are:

1. DB work is misclassified and bypasses review, human approval, or verify.
2. Planning assumes that a missing index implies adding an index without comparing no change, query changes, normalization, denormalization, or physical design.
3. Review uses staging schema instead of current production schema.
4. A validation command reads production rows, stores raw DDL/data/credentials, or creates load on the production primary.
5. Query/schema/ERD changes proceed without result equivalence, integrity, migration, or rollback evidence.

## Scope

### Included

- database-change signal detection from concept and plan artifacts;
- automatic, audited promotion to `high_risk`;
- project-owned external commands for production schema, verify, and local-data test evidence;
- `awf wf db-check --stage plan|verify|test`;
- sanitized canonical evidence at `.workflow/artifacts/database-validation-evidence.json`;
- structured DB decision artifact at `.workflow/artifacts/database-decision.json`;
- mandatory non-skippable G1/G5/G6 DB conditions;
- phase and agent contracts for production schema, comparative choices, equivalence, normalization/denormalization, and local production-shaped testing.

### Excluded

- built-in MySQL/PostgreSQL/warehouse/DuckDB drivers;
- direct production-primary query execution;
- raw production row or DDL persistence;
- AWF-managed masking implementation;
- a general planning-options selection CLI;
- automatic index recommendations.

Projects own engine access and masking scripts. AWF owns command safety, normalized evidence, freshness, hashes, and gates.

## Database-change signal

A change is database-affecting when any strong signal is present in `concept.md`, `spec.md`, `plan.md`, `tasks.md`, `test-criteria.md`, or `allowed-files.json`.

Standalone strong text signals include SQL syntax, migrations, ERD, normalization/denormalization, database engines, warehouses, DuckDB, and their Korean equivalents. Ambiguous terms such as schema, table, column, query, index, and model require a relational anchor on the same line so OpenAPI schemas, URL query strings, HTML tables, ML models, and frontend `index.ts` remain non-DB work.

Strong paths include SQL/Prisma schema files and migration, entity, repository, database, and schema directories. Generic filenames such as `index.ts` are not DB signals by themselves.

The detector returns bounded semantic reasons with raw paths replaced by category and hash, plus a canonical `snapshot_hash` over all classification inputs. When a signal is present, `db-check --stage plan` atomically changes workflow `changeClass` to `high_risk` before running project commands and records a history event containing the sanitized reasons. The same core detection invokes the promotion callback, so classification cannot change between CLI preflight and evidence publication. G1 independently requires plan DB evidence, so omitting the command cannot silently preserve a small workflow.

## Project manifest

`manifest.json` gains an additive profile:

```json
{
  "database_validation": {
    "enabled": false,
    "schema_command": [],
    "verify_command": [],
    "test_command": [],
    "command_timeout_seconds": 30,
    "max_schema_age_hours": 24,
    "allow_production_replica_sample": false
  }
}
```

Commands are non-empty string arrays and run with `shell=False` in the repository root. Credentials remain in the project command's environment; manifest values may name environment variables but may not contain DSNs, passwords, tokens, or secret values.

If a DB signal exists and the profile is disabled, incomplete, or unsafe, `db-check` and the relevant gate fail.

## Database decision artifact

Plan creates `.workflow/artifacts/database-decision.json` before `db-check --stage plan`.

```json
{
  "schema_version": 1,
  "status": "selected",
  "change_surfaces": ["query", "index", "column", "erd"],
  "baseline_option_id": "maintain-current",
  "recommended_option_id": "rewrite-query",
  "selected_option_id": "rewrite-query",
  "candidates": [
    {
      "id": "maintain-current",
      "kind": "maintain",
      "applicable": true,
      "unavailable_reason": null,
      "summary": "Keep the current query and schema",
      "equivalence_plan": "Baseline",
      "integrity_plan": "Verify current constraints",
      "normalization_assessment": "No model change",
      "denormalization_assessment": null,
      "physical_design_assessment": null,
      "read_write_cost": "Measure production-shaped workload",
      "operational_risks": [],
      "transition_risks": [],
      "rollback_or_exit": "No change"
    }
  ],
  "recommendation_rationale": "Selected option meets correctness and load budgets with the lowest lifecycle cost"
}
```

Rules:

- two or three materially different candidates, where canonical candidate content other than ID must differ;
- a `maintain` baseline candidate is mandatory;
- candidate kinds are `maintain`, `query_change`, `physical_design`, `normalize`, and `denormalize`;
- every candidate declares `unavailable_reason`, `denormalization_assessment`, and `physical_design_assessment`; non-applicable candidates require a concrete unavailable reason;
- recommendation and selection IDs must reference applicable candidates;
- no candidate may be selected without an equivalence and integrity plan;
- column, constraint, or ERD surfaces require a non-empty normalization assessment;
- `denormalize` candidates require a structured `denormalization_assessment` with `source_of_truth`, `consistency_window`, `reconciliation`, and `rollback`; other kinds set it to null;
- `physical_design` candidates require a structured `physical_design_assessment` with `read_benefit`, `write_amplification`, `storage`, `build_or_lock`, and `rollback`; other kinds set it to null.

The validator does not assign a winning score. Correctness and integrity are hard gates; the planner records comparative lifecycle evidence and the user's selected option.

## External command contracts

All commands print exactly one JSON object to stdout. AWF rejects malformed JSON, duplicate keys, unknown top-level fields, forbidden sensitive fields at any depth, oversized output, non-zero exit, timeout, and target-policy violations.

Forbidden keys include DSN, URL, password, token, credential, secret, DDL, rows, records, samples, and raw data aliases. Evidence stores hashes and aggregate counts only.

### Production schema command

Required for every stage when a DB signal exists:

```json
{
  "schema_version": 1,
  "kind": "production_schema",
  "target_class": "production_metadata",
  "read_only": true,
  "schema_only": true,
  "engine": "mysql",
  "engine_version": "8.0",
  "captured_at": "2026-08-24T00:00:00Z",
  "schema_hash": "<sha256>",
  "object_counts": {
    "tables": 1,
    "columns": 8,
    "indexes": 2,
    "constraints": 3
  }
}
```

`target_class` must be `production_metadata`. Row access and executable production queries are prohibited. Evidence older than `max_schema_age_hours` fails.

### Verify command

Required at verify:

```json
{
  "schema_version": 1,
  "kind": "database_verify",
  "production_schema_hash": "<sha256>",
  "selected_option_id": "rewrite-query",
  "engine": "mysql",
  "execution_target": "local_same_engine",
  "production_primary_queries": false,
  "raw_production_rows": false,
  "equivalence": "pass",
  "integrity": "pass",
  "query_plan": "pass",
  "migration": "not_applicable",
  "rollback": "pass"
}
```

`equivalence` and `integrity` must pass. Query-only work requires query-plan evidence. Column/constraint/ERD/normalization work requires migration and rollback evidence. Values may be `pass`, `fail`, or `not_applicable`; `not_applicable` is accepted only when the change surfaces make that check irrelevant.

Verify evidence must include `engine`, `execution_target`,
`production_primary_queries`: false, and `raw_production_rows`: false.
`execution_target` is `local_same_engine` or `approved_read_replica`. Index and
structural work require `local_same_engine` and the production schema engine.
Only query planner work may use `approved_read_replica`; DuckDB, cross-engine
execution, and the production primary are prohibited.

### Test command or waiver

The preferred path runs masked production-shaped data locally in the same engine, DuckDB, or both:

```json
{
  "schema_version": 1,
  "kind": "database_test",
  "production_schema_hash": "<sha256>",
  "selected_option_id": "rewrite-query",
  "local_target": "both",
  "masked": true,
  "raw_production_rows": false,
  "equivalence": "pass",
  "integrity": "pass",
  "performance": "pass"
}
```

If `profile.test_command` is absent, `database-decision.json` must contain a `local_data_test_waiver` with non-empty reason, approver, and timestamp. The test gate reports limited confidence but may pass the DB-specific condition because the high-risk workflow still requires G3 approval and full manual test range.

A configured test command may use an approved warehouse, sanitized snapshot, or explicitly enabled read replica. It may never benchmark the production primary. Raw local files remain outside workflow artifacts in ignored, access-controlled storage with project-owned retention.

## Canonical evidence

`db-check` atomically writes `.workflow/artifacts/database-validation-evidence.json`:

```json
{
  "schema_version": 1,
  "database_signal": true,
  "signal_reasons": [],
  "signal_hash": "<sha256>",
  "change_class": "high_risk",
  "profile_hash": "<sha256>",
  "decision_hash": "<sha256>",
  "stages": {
    "plan": {
      "status": "pass",
      "checked_at": "2026-08-24T00:00:00Z",
      "schema": {}
    }
  }
}
```

Later stages preserve prior stage records and add their own sanitized command result and hashes. Every stage record binds its own signal, profile, and decision hashes. A stage is valid only when those identities match the current workflow inputs and the production schema evidence is fresh. `db-check` re-detects the signal under the evidence lock before publication and fails without writing if the snapshot changed during a command. A plan refresh accepts stale or changed prior identity, replaces plan evidence, and removes verify/test whenever signal, profile, decision, or production schema identity changed.

When no DB signal exists, `db-check` returns `not_applicable` and does not promote risk. G1/G5/G6 record a passing not-applicable condition without requiring profile commands.

## Gate integration

DB conditions are evaluated outside `RISK_INVESTMENT.skip_checks`.
- G1 requires DB signal classification, actual workflow `changeClass=high_risk`, a matching `database_risk_escalated` audit event, a valid decision artifact, and current plan-stage schema evidence.
- G5 requires the same actual high-risk state plus current production schema and selected-option equivalence, integrity, applicable query-plan/migration, and rollback PASS.
- G6 requires the same actual high-risk state plus current schema and local test PASS, or a valid explicit local-data waiver.

A generic worker PASS cannot override a failed DB condition. Missing, malformed, stale, state-mismatched, or hash-mismatched evidence fails closed.

## Operator flow

```sh
awf wf db-check --stage plan --repo-root <repo-root> --json
awf wf gate plan --repo-root <repo-root> --json

awf wf db-check --stage verify --repo-root <repo-root> --json
awf wf gate verify --repo-root <repo-root> --result-file <verify-result> --json

awf wf db-check --stage test --repo-root <repo-root> --json
awf wf gate test --repo-root <repo-root> --result-file <test-result> --json

```

For `wf next` auto-apply and `wf apply-result`, the deterministic host checks whether verify/test DB evidence is current and runs the corresponding `db-check` before applying the worker result and evaluating G5/G6. Workers do not write canonical evidence. Manual phase operation may use the explicit commands above with the actual result path emitted by `wf next`.

Plan/verify/test Skills must run `db-check` before their deterministic gate. They must not replace evidence with prose.

## Failure behavior

- missing production schema evidence: fail and preserve workflow;
- DB signal with small/standard class: promote to high risk at plan and record history;
- unsafe production target or sensitive output: fail without persisting the output;
- schema drift after plan: verify/test fail until evidence and decision are refreshed;
- failed equivalence or integrity: route to plan or implementation, never recommend an index automatically;
- failed query plan, migration, rollback, or local parity: block the gate;
- unavailable production-shaped local data: require explicit waiver and report limited confidence.

## Tests

Use hermetic Python commands and temporary workflow roots. Cover:

- concept and artifact DB signal detection without false-positive `index.ts`;
- plan DB signal promoting a small workflow to high risk and preventing phase skips;
- disabled/incomplete profile failures;
- command argv execution without a shell, timeout, output-size, duplicate-key, and sensitive-field rejection;
- production metadata target/read-only/schema-only/freshness validation;
- two-to-three candidates, maintain baseline, selected/recommended IDs, equivalence/integrity, normalization/denormalization/physical-design assessments;
- plan/verify/test evidence hashes and stale/hash mismatch;
- G1/G5/G6 DB conditions remaining mandatory under small/standard skip policies;
- verify surface-specific query-plan/migration/rollback conditions;
- masked local test evidence, unsafe raw-row evidence, and explicit waiver;
- phase Skill, agent-card, CLI, reference, and evidence schema semantic consistency.

## Rollout

The manifest profile defaults disabled. Repositories without a DB signal remain unaffected. A DB signal fails closed until the repository configures safe external commands and a decision artifact. This is intentional P0 behavior: staging-only review is no longer accepted as implicit evidence for production DB work.
