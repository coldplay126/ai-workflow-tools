# Planning options design

## Goal

Stop AWF planning from converging on one implementation prematurely or delegating every detail back to the user. The planner must explore materially different approaches, compare work and transition risks, recommend one option first, and pause only when the decision changes observable scope or risk.

The selected option becomes durable workflow provenance. Changing it invalidates every derived artifact and gate from Plan onward.

## Behavior policy

The planner owns reversible implementation details and follows repository conventions without asking. A user decision is material only when options differ on at least one of these axes:

- external behavior, API, or UX;
- compatibility, data migration, rollout, or rollback;
- security, privacy, reliability, or operational SLO;
- affected scope, test cost, or delivery risk;
- difficult-to-reverse lifecycle cost.

Code style, equivalent internal decomposition, routine library choice, and other reversible implementation details do not create a material decision.

The planner creates two options by default. A third is allowed only when it adds a distinct risk axis and can realistically be selected. More than three options fail validation. If one approach dominates every relevant axis, the planner records why no material decision is required and continues without interrupting the user.

For a material decision:

1. generate two or three options;
2. put the recommended option first;
3. separate work risks from transition risks;
4. state affected work, acceptance impact, and rollback/exit;
5. recommend one option with concrete rationale;
6. ask once using stable decision and option IDs;
7. accept a non-recommended selection without re-arguing;
8. regenerate all derived planning artifacts from the selection.

## Manifest compatibility

New workflows receive:

```json
{
  "planning_options": {
    "required": true
  }
}
```

Existing manifests without `planning_options` are legacy-compatible: G1 does not require the artifact unless it already exists. This avoids breaking active workflows created before rollout. A malformed profile fails closed.

## Artifact

Plan writes `.workflow/artifacts/planning-options.json`.

### No material decision

```json
{
  "schema_version": 1,
  "status": "no_decision_required",
  "no_decision_reason": "Repository conventions determine the only viable implementation without changing external behavior or transition risk.",
  "decisions": [],
  "selection_history": []
}
```

### Material decision awaiting selection

```json
{
  "schema_version": 1,
  "status": "selection_required",
  "no_decision_reason": null,
  "decisions": [
    {
      "id": "D-001",
      "question": "Which rollout model should the feature use?",
      "materiality_axes": ["compatibility_migration", "security_slo"],
      "options": [
        {
          "id": "O-001",
          "summary": "Use a compatibility-preserving dual-read rollout",
          "affected_work": ["service", "migration", "observability"],
          "acceptance_delta": "Requires shadow parity before cutover",
          "work_risks": ["Additional implementation and test paths"],
          "transition_risks": ["Temporary dual-state reconciliation"],
          "rollback_or_exit": "Disable new reads and retain the old source of truth"
        }
      ],
      "recommended_option_id": "O-001",
      "recommendation_rationale": "It preserves rollback while proving parity before cutover.",
      "selected_option_id": null,
      "selected_by": null,
      "selected_at": null
    }
  ],
  "selection_history": []
}
```

### Selected

After every material decision has a selection, status is `selected`. Each decision records a valid option ID, actor, and UTC timestamp. `selection_history` contains append-only entries:

```json
{
  "decision_id": "D-001",
  "previous_option_id": null,
  "selected_option_id": "O-002",
  "selected_by": "steven",
  "selected_at": "2026-08-24T10:00:00Z",
  "source": "cli"
}
```

## Validation

The strict validator requires:

- `schema_version` is the integer `1`, never boolean/string/float;
- exact top-level and nested fields;
- UTF-8, bounded file size, duplicate-key rejection, no symlinks;
- status-specific no-decision/decision rules;
- decision IDs match `D-[0-9]{3}` and are unique;
- option IDs match `O-[0-9]{3}` within each decision and are unique;
- two or three options per material decision;
- options are materially distinct after normalized substantive-field comparison;
- the recommended option exists and is the first option;
- recommendation rationale and risk/rollback fields are non-empty;
- materiality axes are non-empty values from the allowed enum;
- `selection_required` has at least one unselected decision;
- `selected` has every decision selected with actor/timestamp;
- selected IDs reference existing options;
- selection history matches the current selections and has monotonic timestamps.

Text is bounded, single-line normalized, Markdown-safe when rendered, and rejected when it contains credentials, secrets, URLs with userinfo, or raw data markers.

## G1 integration

When `manifest.planning_options.required` is true, G1 requires the artifact.

Stable conditions:

```text
planning_options.artifact
planning_options.shape
planning_options.selection
planning_options.recommendation
planning_options.materiality
```

- `no_decision_required` passes with a concrete reason.
- `selection_required` fails `planning_options.selection` with `decision_selection_required`.
- `selected` passes only when every selection is valid.
- Missing, malformed, inconsistent, or stale selection data fails closed.

The planning-options conditions are additive to the existing artifact, clarification, task, FR coverage, constitution, and DB safety conditions.

Legacy workflows with no profile and no artifact receive a passing `legacy_not_required` condition.

## Plan pause and resume

When material selection is required, the planner:

1. writes `planning-options.json` with `selection_required`;
2. does not finalize spec/plan/tasks/test-criteria/allowed-files as approved artifacts;
3. returns an escaped result with `recommended_action: user_decision` and the decision IDs;
4. enters Plan `deciding` state through the existing orchestrator decision path.

The user selects through:

```sh
awf wf select-option \
  --decision-id D-001 \
  --option-id O-002 \
  --actor steven \
  --repo-root . \
  --json
```

For initial Plan selection, the command atomically updates the artifact and resumes Plan with `continue_workflow`. The next Plan pass uses selected options as authoritative context and writes the five final Plan artifacts.

## Selection changes

A selected option may be changed with the same command.

- If Plan is still deciding, update the selection and continue Plan.
- If G1 or any later phase has run, update selection first, then call `replan_workflow(current_phase, "plan")`.
- Replan invalidates G1–G6, G3 scope hash, phase runtime markers, and every downstream phase status.
- Existing spec/plan/tasks/test-criteria/allowed-files are no longer authoritative and must be regenerated.
- Selection history preserves old and new option IDs.
- A no-op re-selection returns `reuse` and does not increment replan count.

Changing a selection forward without replan is prohibited.

## Selection command safety

`awf wf select-option`:

- canonicalizes the repository root once;
- reads/writes through a `.workflow/artifacts` dirfd with `O_NOFOLLOW`;
- uses an owner-validated, single-link lock file;
- validates the entire artifact before and after mutation;
- creates a random `O_EXCL` 0600 temporary file, fsyncs, and replaces by dirfd;
- requires non-empty actor and stable IDs;
- never accepts free-form JSON patches;
- updates state only after durable artifact publication;
- reconciles a durable artifact update if state transition fails, using selection history as provenance;
- emits no option body or user text in JSON output, only IDs, status, action, and hashes.

## Planner contracts

The phase Plan Skill, spec-writer source/generated agents, multi-agent protocol, and plan card must share one canonical artifact contract.

The planner must:

- investigate before producing options;
- use repository facts to eliminate fake choices;
- present recommendation first;
- distinguish work risk and transition risk;
- avoid asking reversible implementation details;
- write `no_decision_required` instead of inventing alternatives;
- stop at `selection_required` instead of choosing for the user;
- use an existing selected artifact as source of truth on rerun;
- regenerate all downstream artifacts after a selection change.

## Tests

Use hermetic workflow roots. Cover:

- strict artifact schemas for all statuses;
- two/three option limits, IDs, recommendation-first, selected references;
- materially identical options disguised by IDs/whitespace/order;
- no-decision reason;
- G1 missing/malformed/unselected/selected/legacy behavior;
- initial selection, alternative selection, and multiple decisions;
- actor/timestamp/history persistence;
- no-op re-selection;
- post-G1 selection change resetting phases, gates, and G3 scope hash;
- failure windows between artifact write and state transition;
- symlink/hardlink/concurrent selection protection;
- planner escape→deciding→select→continue→G1 smoke;
- source/generated agent parity and Ask capability;
- semantic command and artifact contract consistency.

## Non-goals

- a new workflow phase before Plan;
- more than three options;
- AI scoring that replaces user choice;
- asking about routine reversible implementation details;
- automatic re-selection after user choice;
- embedding full option text in state.json;
- merging planning options with the DB-specific decision artifact in this change.
