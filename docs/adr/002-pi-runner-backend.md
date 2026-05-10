# ADR-002: Pi Runner Backend Integration Boundary

## Status

**Accepted** (2026-05-10)

## Context

Pi is a minimal terminal coding harness with a small core, extension packages,
skills, prompt templates, provider switching, and tree-structured sessions.
Those strengths are complementary to awf, but they are not the same layer.

awf owns deterministic workflow control:

- `.workflow/state.json` is the canonical workflow state.
- `.ai-context/` is the canonical analysis artifact layer.
- `.awf-operations/` is the canonical operations memory.
- `awf ready --gate` is the canonical automation gate.

cmux-agent already owns local multi-surface orchestration. It can spawn and
manage worker terminals for parallel or chained dispatch.

## Decision

Treat Pi as a planned runner backend / per-worker harness, not as a provider and
not as a replacement for cmux-agent.

Layering:

```text
awf = workflow/gate/control plane
cmux-agent = multi-surface runtime
Pi = per-worker terminal harness
```

The first implementation step is detection plus a print-mode adapter skeleton:

- `awf doctor` reports whether the `pi` command is available.
- `awf ready --json` includes the same Pi runner readiness payload under
  `doctor.runners`.
- `AWF_PI_COMMAND` can point detection at a non-default executable name.
- `awf.runners.pi.run_pi_print()` wraps `pi --no-session -p <prompt>` and
  normalizes return code, stdout, stderr, timeout, and elapsed time into the
  existing awf result shape.
- Pi is not added to `available_surfaces` until an awf execution adapter exists.

## Consequences

Positive:

- Pi can be integrated without installing it in CI.
- cmux-agent remains the process/surface manager.
- awf state, gate, and operations memory stay canonical.
- Future work can wrap Pi print/JSON/RPC mode behind `ResultEnvelope` without
  changing workflow contracts.

Negative:

- Pi session state and awf workflow state will both exist once execution is
  added; awf must record Pi session IDs as provenance, not as canonical state.
- Pi permissions or extensions cannot replace `awf ready --gate`.
- The adapter skeleton is intentionally not wired into workflow execution yet;
  detection still only proves that Pi is available on PATH.

## Follow-up

1. Decide whether workflow execution should use Pi print/JSON mode or RPC mode.
2. Add a real `awf` dispatch/backend selector for Pi after the result contract is stable.
3. Record Pi session/export IDs in `.awf-operations` events as provenance when sessions are enabled.
4. Keep live Pi execution smoke tests outside the default CI suite.
