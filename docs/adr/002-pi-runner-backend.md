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

The first implementation step is detection plus a print-mode adapter skeleton
that can already normalize Pi output into awf's existing result shapes:

- `awf doctor` reports whether the `pi` command is available.
- `awf ready --json` includes the same Pi runner readiness payload under
  `doctor.runners`.
- `AWF_PI_COMMAND` can point detection at a non-default executable name.
- `awf.runners.pi.run_pi_print()` wraps `pi --no-session -p <prompt>` and
  normalizes return code, stdout, stderr, timeout, and elapsed time into the
  existing awf result shape.
- `awf.runners.pi.run_pi_agent()` converts one Pi print-mode worker response
  into `AgentResult`, including role, JSON parse status, timeout status, and
  parsed findings when requested.
- `dispatch.surface_preference = "pi"` selects a Pi-backed dispatch surface
  only when the Pi command is installed. `auto` still chooses only the existing
  inline/cmux surfaces.

## Consequences

Positive:

- Pi can be integrated without installing it in CI.
- cmux-agent remains the multi-surface runtime; Pi opt-in dispatch is a
  separate per-worker harness path.
- awf state, gate, and operations memory stay canonical.
- Pi print mode can now be exercised through the same `MultiAgentDispatch`
  contract as inline/cmux, without changing workflow result contracts.

Negative:

- Pi session state and awf workflow state will both exist once execution is
  added; awf must record Pi session IDs as provenance, not as canonical state.
- Pi permissions or extensions cannot replace `awf ready --gate`.
- The first dispatch adapter uses print mode and no saved Pi sessions, so it
  proves the execution contract but does not yet capture Pi's full session-tree
  advantage.

## Follow-up

1. Decide whether the long-lived Pi backend should stay on print mode or move
   to JSON/RPC mode for richer event capture.
2. Record Pi session/export IDs in `.awf-operations` events as provenance when sessions are enabled.
3. Keep live Pi execution smoke tests outside the default CI suite.
