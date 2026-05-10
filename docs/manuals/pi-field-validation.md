# Pi Field Validation

This checklist answers one question: does Pi improve awf field operation, or is
it only a technical integration?

Pi is not part of default CI. Default CI uses fake binaries and contract tests
so the repository stays deterministic. Field validation must run on a machine
with Pi plus a configured provider.

## Preconditions

- Node/npm are available.
- Pi is installed, or `--npm-exec` is allowed for a temporary npm execution.
- Pi has provider authentication through `/login` or an API key environment
  variable such as `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

Current package scope:

```bash
npm view @earendil-works/pi-coding-agent version
```

Legacy package scope:

```bash
npm view @mariozechner/pi-coding-agent version
```

As of the 2026-05-07 Pi migration, the new package scope is
`@earendil-works/pi-coding-agent`. The CLI remains `pi`.

## Smoke

Use a globally installed Pi:

```bash
cd ~/Documents/GitHub/ai-workflow-tools
python3 cli/tests/run_pi_field_smoke.py --json --write-result
```

Use npm without global installation:

```bash
cd ~/Documents/GitHub/ai-workflow-tools
python3 cli/tests/run_pi_field_smoke.py --npm-exec --json --write-result
```

Expected pass:

- `ok: true`
- `pi_version.ok: true`
- `pi_dispatch.ok: true`
- `pi_dispatch.conclusion: "PASS"`
- `pi_dispatch.parse_error: false`
- `reason: "dispatch_ok"` or `reason: "dispatch_ok_with_anthropic_extra_usage"`

Expected blocked states:

- `reason: "pi_not_found"`: Pi is not installed and `--npm-exec` was not used.
- `reason: "missing_provider_auth"`: Pi runs, but provider authentication is
  not configured.
- `reason: "provider_quota_exhausted"` with
  `billing_context: "anthropic_extra_usage"`: Pi reached Anthropic, but Claude
  Extra Usage is exhausted. This is the expected failure when Anthropic
  subscription auth is active and no Extra Usage budget remains.
- `reason: "provider_auth_failed"`: Pi reached the provider, but the provider
  rejected the credential.
- `reason: "provider_rate_limited"`: Pi reached the provider, but the request
  was rate-limited.
- `reason: "provider_contract_parse_error"`: Pi/provider responded, but the
  result contract is not stable enough for awf dispatch.

The JSON payload includes `diagnosis.summary` and `next_action` so field
operators can distinguish local installation problems from provider auth,
quota, billing, and output-contract problems.

When `--write-result` is used, the latest result is stored at
`.awf-operations/pi-field-smoke/latest.json`. `awf doctor --repo-root . --json`
and `awf ready --repo-root . --json` summarize that latest result under
`pi_readiness.last_field_smoke`, including `ok`, `reason`, and `recorded_at`.
`awf ready` treats results older than 24 hours as stale and recommends a fresh
field smoke before relying on Pi dispatch. Blocking reasons such as
`provider_quota_exhausted` or `missing_provider_auth` are also promoted into
`recommended_next` so Pi remains opt-in until the provider path is fixed.

### Anthropic subscription auth

Pi may use Anthropic subscription auth from a Claude Pro/Max account. In that
mode, third-party harness calls are billed through Claude Extra Usage instead
of Claude plan limits. A failure such as `You're out of extra usage` means the
Pi command and awf dispatch path reached Anthropic, but the account needs Extra
Usage enabled/increased or a different provider/API key for Pi runs.

## Field Comparison

After smoke passes, compare Pi against the existing surface on the same small
workflow task:

```bash
# Baseline
awf wf next --repo-root . --phase review --provider codex --dry-run

# Pi opt-in dispatch
awf wf next --repo-root . --phase review --provider codex --dry-run
# with .workflow/provider-config.json containing:
# {
#   "dispatch": {
#     "surface_preference": "pi"
#   }
# }
```

Record:

- success/failure
- elapsed seconds
- JSON parse success
- generated artifacts
- whether the output is easier to inspect or resume

Pi is useful only if it improves at least one field property without harming
the existing gate/state contract:

- better worker isolation
- better session traceability
- better extension/package customization
- lower operator friction for repeated worker runs

If it only adds another provider invocation path, keep it opt-in and do not move
it into `auto`.
