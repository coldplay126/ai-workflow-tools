# AWF Supervisor AWS Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the durable AWS Supervisor API, lease store, router, agent authentication, command queue, and reconciliation loop without duplicating the existing OMP launcher lifecycle authority.

**Architecture:** A dedicated Supervisor SAM application stores jobs, agents, events, and artifacts. Admin routes use AWS IAM; local-agent routes use revocable enrollment credentials exchanged for short-lived opaque tokens; EC2-agent routes use the instance role. The router invokes the existing launcher Lambda through an internal IAM-protected event when EC2 must start.

**Tech Stack:** AWS SAM/CloudFormation, Node.js 22, AWS SDK v3, DynamoDB, SQS, S3, KMS, API Gateway HTTP API, Lambda, EventBridge, `node:test`, AJV

---

## Preconditions

- Complete and merge `aws-agent-poc/docs/superpowers/plans/2026-07-30-cloudflare-omp-remote-access.md` first. This plan requires `launcher/src/handler.js`, `launcher/src/instance-state.js`, and the launcher Lambda resources.
- At plan creation, `/Users/steven/Documents/GitHub/aws-agent-poc` has no `.git` metadata. Before implementation, put it under version control without committing local state:

```bash
cd /Users/steven/Documents/GitHub/aws-agent-poc
printf '\n.cmux/\n*.bak-*\n' >> .gitignore
git init -b main
git add . ':!.env'
git commit -m "chore: establish aws agent poc baseline"
```

- Use a dedicated `aws-agent-poc` worktree after repository initialization.
- The AWF core-contract plan must be merged or available at `AWF_SOURCE_DIR`.
- Do not run a live AWS deployment until local Node tests and `sam validate --lint` pass.

## File map

- `supervisor/src/domain.js`: exact state, transition, fencing, and validation rules.
- `supervisor/src/store.js`: DynamoDB conditional operations.
- `supervisor/src/admin-handler.js`: submit, status, decisions, enrollment, agent list.
- `supervisor/src/agent-handler.js`: heartbeat, claim, renewal, events, command poll.
- `supervisor/src/local-authorizer.js`: short-lived local-agent access-token validation.
- `supervisor/src/router.js`: local-first assignment, AWS wake request, stale reconciliation.
- `supervisor/src/lifecycle-client.js`: internal launcher Lambda invocation.
- `supervisor/contracts/`: four pinned JSON schemas plus the pinned state-machine fixture copied from `ai-workflow-tools`.
- `scripts/sync-awf-supervisor-contracts.sh`: deterministic contract sync and digest.
- `infra/template.yaml`: SAM resources and least-privilege policies.

### Task 1: Pin the shared contract and create the Supervisor package

**Files:**
- Create: `supervisor/package.json`
- Create: `supervisor/package-lock.json`
- Create: `supervisor/src/contracts.js`
- Create: `supervisor/test/contracts.test.js`
- Create: `supervisor/contracts/job-v1.json`
- Create: `supervisor/contracts/agent-v1.json`
- Create: `supervisor/contracts/event-v1.json`
- Create: `supervisor/contracts/command-v1.json`
- Create: `supervisor/contracts/state-machine-v1.json`
- Create: `supervisor/contracts/manifest.json`
- Create: `scripts/sync-awf-supervisor-contracts.sh`

- [ ] **Step 1: Write the failing contract loader test**

```js
import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { validateContract } from "../src/contracts.js";

const validJob = JSON.parse(await readFile(new URL("./fixtures/job-v1.json", import.meta.url), "utf8"));

test("accepts the pinned version 1 job fixture", () => {
  assert.deepEqual(validateContract("job", validJob), validJob);
});

test("rejects an unsupported major version", () => {
  assert.throws(
    () => validateContract("job", { ...validJob, schema_version: 2 }),
    /unsupported job schema_version: 2/,
  );
});
```

Add fixtures for all four contracts and a negative fixture with duplicate repo names.

- [ ] **Step 2: Confirm RED**

```bash
cd supervisor
npm test
```

Expected: the package or contract module is missing.

- [ ] **Step 3: Create the package and deterministic sync script**

`package.json` must set `"type": "module"`, `"engines": {"node": ">=22"}`, and these scripts:

```json
{
  "scripts": {
    "test": "node --test",
    "test:coverage": "node --test --experimental-test-coverage"
  },
  "dependencies": {
    "@aws-sdk/client-dynamodb": "^3.850.0",
    "@aws-sdk/client-lambda": "^3.850.0",
    "@aws-sdk/client-s3": "^3.850.0",
    "@aws-sdk/client-sqs": "^3.850.0",
    "@aws-sdk/lib-dynamodb": "^3.850.0",
    "ajv": "^8.17.1",
    "ajv-formats": "^3.0.1"
  },
  "devDependencies": {
    "yaml": "^2.8.1"
  }
}
```

The sync script must require `AWF_SOURCE_DIR`, copy exactly four schema files, and write a sorted manifest:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
: "${AWF_SOURCE_DIR:?Set AWF_SOURCE_DIR to the ai-workflow-tools checkout}"
source_dir="$AWF_SOURCE_DIR/cli/src/awf/supervisor/schemas"
target_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/supervisor/contracts"
mkdir -p "$target_dir"
for name in agent command event job; do
  install -m 0644 "$source_dir/$name-v1.json" "$target_dir/$name-v1.json"
done
install -m 0644 "$AWF_SOURCE_DIR/cli/src/awf/supervisor/fixtures/state-machine-v1.json" "$target_dir/state-machine-v1.json"
python3 - "$target_dir" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
files = {}
for path in sorted(root.glob("*-v1.json")):
    files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
(root / "manifest.json").write_text(json.dumps({"schema_version": 1, "files": files}, indent=2, sort_keys=True) + "\n")
PY
```

- [ ] **Step 4: Implement AJV validation and semantic checks**

```js
const validators = new Map();

export function validateContract(kind, payload) {
  if (!Object.hasOwn(schemaPaths, kind)) throw new Error(`unknown supervisor contract: ${kind}`);
  if (!Number.isInteger(payload?.schema_version) || payload.schema_version !== 1) {
    throw new Error(`unsupported ${kind} schema_version: ${String(payload?.schema_version)}`);
  }
  const validate = validators.get(kind) ?? compile(kind);
  if (!validate(payload)) throw new Error(`invalid ${kind} contract: ${formatAjvError(validate.errors)}`);
  if (kind === "job") assertUniqueRepoNames(payload.repo_refs);
  return structuredClone(payload);
}
```

Verify every entry in `manifest.json` before compiling schemas. Require exactly the four versioned schemas plus `state-machine-v1.json`; a missing, extra, or digest-mismatched file must fail cold start.

- [ ] **Step 5: Sync, install, and run GREEN**

```bash
AWF_SOURCE_DIR=/Users/steven/Documents/GitHub/ai-workflow-tools ./scripts/sync-awf-supervisor-contracts.sh
cd supervisor
npm install
npm test
```

Expected: all contract tests pass.

- [ ] **Step 6: Commit**

```bash
git add supervisor scripts/sync-awf-supervisor-contracts.sh
git commit -m "feat: pin supervisor control plane contracts"
```

### Task 2: Implement the state machine and fencing expressions

**Files:**
- Create: `supervisor/src/domain.js`
- Test: `supervisor/test/domain.test.js`

- [ ] **Step 1: Write failing transition, lease, and expiry tests**

```js
test("fences a stale owner generation", () => {
  assert.throws(() => assertOwnerWrite({
    expectedAgentId: "local-1",
    expectedGeneration: 5,
    actualAgentId: "local-1",
    actualGeneration: 4,
    leaseExpiresAt: "2026-07-30T12:01:00Z",
    now: "2026-07-30T12:00:00Z",
  }), /generation/);
});

test("running without a checkpoint becomes recovery-required", () => {
  assert.equal(reconcileExpiredRunning({ checkpoint: null, stopped: false }), "RECOVERY_REQUIRED");
});
```

Load `supervisor/contracts/state-machine-v1.json` and execute every transition and recovery vector in both tests and implementation; do not hand-copy a second adjacency table from the Python source.

- [ ] **Step 2: Confirm RED**

```bash
cd supervisor && npm test -- --test-name-pattern='fences|recovery-required'
```

Expected: missing exports from `domain.js`.

- [ ] **Step 3: Implement exact tables and DynamoDB condition builders**

```js
export function ownerCondition() {
  return {
    ConditionExpression: [
      "owner_agent_id = :owner",
      "generation = :generation",
      "lease_expires_at > :now",
    ].join(" AND "),
    ExpressionAttributeValues: {
      ":owner": undefined,
      ":generation": undefined,
      ":now": undefined,
    },
  };
}
```

Expose `assertTransition`, `assertOwnerWrite`, `nextGeneration`, `claimUpdate`, `renewUpdate`, `terminalUpdate`, and `reconcileExpiredRunning`. Conditions must compare timestamps generated by the server, not agent clocks.

- [ ] **Step 4: Run GREEN and commit**

```bash
cd supervisor && npm test
cd ..
git add supervisor/src/domain.js supervisor/test/domain.test.js
git commit -m "feat: enforce supervisor control plane fencing"
```

### Task 3: Implement conditional DynamoDB persistence

**Files:**
- Create: `supervisor/src/store.js`
- Test: `supervisor/test/store.test.js`

- [ ] **Step 1: Write failing store tests with a command-capturing document client**

```js
test("claim uses generation and unowned-state conditions", async () => {
  const ddb = new RecordingDocumentClient([{ Attributes: claimedJob }]);
  const store = new SupervisorStore({ ddb, jobsTable: "jobs", agentsTable: "agents", eventsTable: "events" });
  await store.claimJob({ jobId: "job-1", agentId: "local-1", now: NOW, leaseExpiresAt: LEASE });
  const input = ddb.commands[0].input;
  assert.match(input.ConditionExpression, /#state = :queued/);
  assert.match(input.UpdateExpression, /generation = generation \+ :one/);
});
```

Cover submit idempotency, claim race, renewal, stale completion, duplicate event idempotency, heartbeat, token exchange compare-and-set, approval generation, consistent job reads, and a fenced-event transaction race: expire and reclaim a lease between an agent request and `TransactWrite`, then assert that the transaction fails and creates neither the event item nor a terminal job update. A successful claim must generate one canonical UUID4 `dispatch_command_id` and store it in the Jobs item in the same conditional update as owner, generation, and lease; this is an internal persistence field stripped before `job-v1` validation or any API response.

- [ ] **Step 2: Confirm RED**

```bash
cd supervisor && npm test -- --test-name-pattern='claim uses|stale completion'
```

Expected: missing `SupervisorStore`.

- [ ] **Step 3: Implement one method per conditional operation**

Use `DynamoDBDocumentClient` commands. Translate `ConditionalCheckFailedException` into stable `GENERATION_CONFLICT`, `LEASE_CONFLICT`, or `IDEMPOTENCY_CONFLICT` errors after a consistent read. Never retry a conditional conflict blindly.

Event keys must be sortable and generation-scoped:

```js
export function eventSortKey(generation, sequence) {
  return `G#${String(generation).padStart(10, "0")}#S#${String(sequence).padStart(20, "0")}`;
}
```

Add `writeFencedEvent({ event, ownerAgentId, generation, stateUpdate, now })`. It issues exactly one `TransactWriteCommand` and never targets the Jobs item twice: when `stateUpdate` is absent, use (1) a Jobs `ConditionCheck` for `owner_agent_id = :owner AND generation = :generation AND lease_expires_at > :now` and (2) an Events `Put`; when `stateUpdate` is present, replace the ConditionCheck with one Jobs `Update` carrying that same owner/generation/lease condition plus the expected source state, and transact it with the Events `Put`. The Events put uses the generation-scoped key with `attribute_not_exists(PK) AND attribute_not_exists(SK)`. Terminal events and `GATE_EVALUATED/WAITING_APPROVAL` supply `stateUpdate`; ordinary progress events do not. If the event key already exists, consistently read it and return success only when the stored canonical event is byte-for-byte equal; otherwise return `IDEMPOTENCY_CONFLICT`. Translate transaction cancellation from the single Jobs condition into `LEASE_CONFLICT` or `GENERATION_CONFLICT` only after a consistent read.
`SupervisorStore.claimJob` returns the newly claimed generation and stored `dispatch_command_id`. A release to `QUEUED` clears it; every ownership acquisition replaces it. A consistent read of a current `CLAIMED` job may recover that same ID for retry, but no code generates a second command ID for an existing owner/generation.

- [ ] **Step 4: Run GREEN and commit**

```bash
cd supervisor && npm test
cd ..
git add supervisor/src/store.js supervisor/test/store.test.js
git commit -m "feat: persist supervisor jobs and leases"
```

### Task 4: Add the IAM-authenticated admin API

**Files:**
- Create: `supervisor/src/http.js`
- Create: `supervisor/src/admin-handler.js`
- Test: `supervisor/test/admin-handler.test.js`

- [ ] **Step 1: Write failing route and idempotency tests**

Test exact HTTP API payload-v2 method/path pairs:

```text
POST /v1/admin/jobs
GET  /v1/admin/jobs/{job_id}
POST /v1/admin/jobs/{job_id}/cancel
POST /v1/admin/jobs/{job_id}/decisions
GET  /v1/admin/agents
POST /v1/admin/agents/enroll
POST /v1/admin/agents/{agent_id}/wake
```

```js
test("submit stores prompt in KMS S3 and metadata in DynamoDB", async () => {
  const response = await handler(httpEvent("POST", "/v1/admin/jobs", {
    headers: { "idempotency-key": "5a2c1e31-cf45-4fe4-ae47-f40d3eb90837" },
    body: JSON.stringify({
      schema_version: 1,
      workflow_id: "2026-07-30-login-contract",
      requested_target: "auto",
      repo_refs: [{ repo: "blip-server", base: "main" }],
      required_capabilities: ["git", "omp"],
      prompt: "Fix the login contract.\n",
    }),
  }));
  assert.equal(response.statusCode, 201);
  assert.equal(s3.puts[0].input.ServerSideEncryption, "aws:kms");
  assert.equal(JSON.stringify(ddb.puts[0]).includes("Fix the login contract"), false);
});
```

The submit handler must allocate `job_id` before writing the prompt, use the sole prompt key `prompts/${job_id}.txt`, and set `Metadata: { sha256: sha256Hex(promptUtf8) }` on that SSE-KMS object. It accepts only the six version-1 submit fields and rejects a caller-provided `approval_required`; the stored job always gets server-owned `approval_required: true`. The Jobs item and every API response must omit both a prompt URI and a prompt digest; prompt retrieval derives that key from `job_id` and verifies the S3 object metadata digest. Extend the test with a fixed `job_id` to assert the exact key, metadata, and mandatory approval field, and assert that neither the prompt text nor `prompts/job-1.txt` nor its digest occurs in the captured Jobs item or response body.

Also test 64 KiB prompt limit, empty prompt, duplicate repo, missing idempotency key, body/query/path canonicalization, stale generation, no prompt in response logs, cleanup wake, and both cancellation paths. An unowned `QUEUED` or `BLOCKED` job must become `CANCELLED` immediately with explicit server-side no-execution and cleanup evidence plus an immutable idempotency-keyed audit record; a claimed or later job gets only `desired_state=CANCELLED` so its owner emits the terminal event after stopping and cleanup. The wake route accepts only the configured AWS agent ID, an empty body, no query, and a canonical UUID4 `Idempotency-Key`; it asynchronously invokes the Router Lambda with exactly `{source:"awf.supervisor.admin",version:1,operation:"wake-agent",agent_id,request_id:<idempotency-key>}` and returns 202 after invocation acceptance. It never invokes the launcher itself, creates a job, or mutates agent state.

- [ ] **Step 2: Confirm RED**

```bash
cd supervisor && npm test -- --test-name-pattern='submit stores|stale generation'
```

Expected: missing admin handler.

- [ ] **Step 3: Implement stable HTTP responses and request parsing**

```js
export function jsonResponse(statusCode, payload, requestId) {
  return {
    statusCode,
    headers: { "content-type": "application/json", "x-request-id": requestId },
    body: JSON.stringify(payload),
  };
}
```

Reject raw query strings on state-changing routes. `POST /v1/admin/jobs/{job_id}/cancel` accepts an empty body and canonical UUID4 idempotency key. If the job is unowned in `QUEUED` or `BLOCKED`, call the shared state-machine validator with explicit `execution_stopped=true` no-execution evidence and `cleanup_completed=true`, then transact the conditional `CANCELLED` update with one immutable `ADMIN_CANCEL#<idempotency-key>` audit record; an exact retry returns the stored result and a conflicting reuse returns 409. For `CLAIMED` or any later non-terminal state, conditionally set only `desired_state=CANCELLED`; the owner must consume the claimed command even when desired state is cancelled, then emit the schema-valid terminal event with stopped/cleanup proof. `POST /v1/admin/jobs/{job_id}/decisions` accepts exactly `{generation, decision, requested_action}` where the only pairs are `APPROVE/CONTINUE` and `REJECT/CANCEL`. In one DynamoDB transaction, condition on the job being `WAITING_APPROVAL` with `approval_required=true`, the same owner and generation, and an unexpired lease; put one immutable `DECISION#<generation>` record in the Events table containing only job ID, generation, pair, principal ARN, and server timestamp; then set `state=RUNNING, desired_state=RUNNING` for approval or only `desired_state=CANCELLED` for rejection. An exact duplicate returns the existing decision; a conflicting duplicate or stale generation returns 409. Every version-1 job requires this decision; no submit field, required capability, workflow ID, target, or principal may disable it. Neither owned-job cancel nor reject claims terminal cancellation before the agent confirms flat stopped/cleanup proof.

- [ ] **Step 4: Run GREEN and commit**

```bash
cd supervisor && npm test
cd ..
git add supervisor/src/http.js supervisor/src/admin-handler.js supervisor/test/admin-handler.test.js
git commit -m "feat: add supervisor admin API"
```

### Task 5: Add local-agent enrollment and short-lived access tokens

**Files:**
- Create: `supervisor/src/tokens.js`
- Create: `supervisor/src/local-authorizer.js`
- Test: `supervisor/test/tokens.test.js`
- Test: `supervisor/test/local-authorizer.test.js`
- Modify: `supervisor/src/store.js`
- Modify: `supervisor/test/store.test.js`
- Modify: `supervisor/src/admin-handler.js`

- [ ] **Step 1: Write failing enrollment, exchange, expiry, and revoke tests**

```js
test("enrollment returns a refresh token once and stores only its hash", async () => {
  const result = await enrollLocalAgent({ agentId: "local-mac-01", store, randomBytes: fixedRandom });
  assert.equal(result.refresh_token.length > 40, true);
  assert.equal(store.lastAgent.refresh_token_hash, sha256Base64Url(result.refresh_token));
  assert.equal(JSON.stringify(store.lastAgent).includes(result.refresh_token), false);
});

test("authorizer rejects expired access token", async () => {
  const result = await authorizeLocalAgent(tokenEvent("expired"), dependencies);
  assert.deepEqual(result, { isAuthorized: false });
});

test("authorizer retrieves a newly issued opaque token by its hash without a scan", async () => {
  const issued = await exchangeRefreshToken({ agentId: "local-mac-01", refreshToken, store, now: NOW });
  const result = await authorizeLocalAgent(tokenEvent(issued.access_token), { store, now: NOW });
  assert.deepEqual(result, {
    isAuthorized: true,
    context: { agentId: "local-mac-01", environment: "local" },
  });
  assert.equal(store.lastAccessTokenRead.ConsistentRead, true);
  assert.equal(store.lastAccessTokenRead.Key.access_token_hash, tokenHash(issued.access_token));
});

test("revocation removes the keyed token so immediate and later lookups fail", async () => {
  const issued = await exchangeRefreshToken({ agentId: "local-mac-01", refreshToken, store, now: NOW });
  await revokeLocalAgent({ agentId: "local-mac-01", store });
  assert.deepEqual(await authorizeLocalAgent(tokenEvent(issued.access_token), { store, now: NOW }), {
    isAuthorized: false,
  });
});
```

Cover 256-bit randomness, constant-time hash comparison, 15-minute access expiry, refresh revoke, replacement of a prior access token, issue-to-immediate-lookup, expiry, and revocation through the keyed lookup, wrong agent ID, duplicate Authorization headers, and token values absent from logs. Assert that the authorizer sends `GetItem` with `ConsistentRead: true` to the hash-keyed token table and never sends `Scan` or accepts an agent ID from the request.

- [ ] **Step 2: Confirm RED**

```bash
cd supervisor && npm test -- --test-name-pattern='enrollment returns|expired access'
```

Expected: missing token modules.

- [ ] **Step 3: Implement opaque token issuance**

```js
export function newOpaqueToken(randomBytesFn = randomBytes) {
  return randomBytesFn(32).toString("base64url");
}

export function tokenHash(token) {
  return createHash("sha256").update(token, "utf8").digest("base64url");
}
```

Admin enrollment writes a refresh hash and returns the raw refresh token exactly once. `POST /v1/local-agent/token` accepts `{agent_id, refresh_token}`, reads that agent's refresh hash, and compares it in constant time. One `TransactWriteCommand` atomically replaces the 15-minute server-expiry credential: its Agents update has `ConditionExpression` requiring the stored current-access-token hash to equal the hash read before exchange (or attribute-not-exists on first issue), writes the new hash, puts the new `AccessTokens` item keyed by `access_token_hash`, and deletes the prior keyed item when present. On conditional conflict, reread once and repeat refresh-token validation before retry; otherwise return 409. This prevents concurrent exchanges from leaving an orphan valid token. The token item contains only `access_token_hash`, `agent_id`, `environment`, and `expires_at_epoch`; it has DynamoDB TTL on `expires_at_epoch`. Revocation uses one transaction with the same current-hash condition to delete that item and clear the Agents hash. Add a barrier-controlled concurrent exchange test followed by revoke and prove neither losing token authorizes. The API route itself is unauthenticated but inherits the HTTP API's explicit 5 requests/second and burst-10 throttle.

- [ ] **Step 4: Implement the HTTP API simple authorizer**

Return:

```js
return {
  isAuthorized: true,
  context: { agentId: record.agent_id, environment: "local" },
};
```

The handler must take agent identity only from authorizer context, never from a caller-controlled header.

Hash the presented bearer value, use one strongly consistent `GetItem` by `{ access_token_hash }` on `AccessTokens`, require an unexpired server timestamp and `environment === "local"`, then return the context above. The authorizer has no GSI, table scan, agent-id request field, KMS, S3, SQS, or router permission. Apply this same authorizer to every local-agent route, including the long-poll command route; the local Lease API and command poll therefore use the same token broker.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd supervisor && npm test
cd ..
git add supervisor/src/tokens.js supervisor/src/local-authorizer.js supervisor/src/store.js supervisor/src/admin-handler.js supervisor/test/tokens.test.js supervisor/test/local-authorizer.test.js supervisor/test/store.test.js
git commit -m "feat: authenticate local supervisor agents"
```

### Task 6: Add local and AWS agent APIs plus command delivery

**Files:**
- Create: `supervisor/src/agent-handler.js`
- Test: `supervisor/test/agent-handler.test.js`

- [ ] **Step 1: Write failing heartbeat, claim, renewal, event, and poll tests**

Route groups are explicit:

```text
POST /v1/local-agent/token                              NONE, refresh validated in handler
POST /v1/local-agent/heartbeat                          custom local authorizer
GET  /v1/local-agent/commands                           custom local authorizer
GET  /v1/local-agent/jobs/{job_id}                      custom local authorizer
GET  /v1/local-agent/jobs/{job_id}/prompt               custom local authorizer
GET  /v1/local-agent/jobs/{job_id}/checkpoint           custom local authorizer
GET  /v1/local-agent/jobs/{job_id}/decision             custom local authorizer
POST /v1/local-agent/jobs/{job_id}/claim                custom local authorizer
POST /v1/local-agent/jobs/{job_id}/renew                custom local authorizer
POST /v1/local-agent/jobs/{job_id}/transition           custom local authorizer
POST /v1/local-agent/jobs/{job_id}/artifacts            custom local authorizer
POST /v1/local-agent/jobs/{job_id}/events               custom local authorizer
POST /v1/aws-agent/heartbeat                            AWS_IAM
GET  /v1/aws-agent/jobs/{job_id}                        AWS_IAM
GET  /v1/aws-agent/jobs/{job_id}/prompt                 AWS_IAM
GET  /v1/aws-agent/jobs/{job_id}/decision               AWS_IAM
GET  /v1/aws-agent/jobs/{job_id}/checkpoint             AWS_IAM
POST /v1/aws-agent/jobs/{job_id}/claim                  AWS_IAM
POST /v1/aws-agent/jobs/{job_id}/renew                  AWS_IAM
POST /v1/aws-agent/jobs/{job_id}/artifacts              AWS_IAM
POST /v1/aws-agent/jobs/{job_id}/transition             AWS_IAM
POST /v1/aws-agent/jobs/{job_id}/events                 AWS_IAM
```

```js
test("local agent cannot mutate another owner's job", async () => {
  const response = await handler(agentEvent("POST", "/v1/local-agent/jobs/job-1/events", {
    agentId: "local-2",
    body: eventBody({ owner_agent_id: "local-1", generation: 4 }),
  }));
  assert.equal(response.statusCode, 409);
  assert.equal(JSON.parse(response.body).code, "LEASE_CONFLICT");
});

test("accepts the actual HTTP API IAM assumed-role context only for the configured AWS agent role", async () => {
  const response = await handler(payloadV2("POST", "/v1/aws-agent/heartbeat", {
    requestContext: {
      authorizer: {
        iam: {
          accountId: "123456789012",
          userArn: "arn:aws:sts::123456789012:assumed-role/AwfSupervisorAgentRole/i-0123456789abcdef0",
        },
      },
    },
  }));
  assert.equal(response.statusCode, 200);
});
```

Cover heartbeat capability validation, claim acceptance deadline, server-time lease renewal, the fixed `CLAIMED→PREPARING` and `PREPARING→RUNNING` transition route, stale event, duplicate event, a lease-expiry/reclaim race that persists no stale event, local long poll timeout, valid and invalid payload-v2 AWS assumed-role contexts, owner-only job and decision reads, deterministic prompt-key metadata verification, a 1 MiB decoded artifact limit, artifact checksum mismatch, terminal event proof fields, strict event allowlists, acceptance of each fixed summary label, and rejection of arbitrary prompt/source/model-output text in `summary` or any other field. Transition tests must reject an extra body field, wrong owner/generation/current state, expired lease, skipped state, and any target outside those two exact pairs before a workspace or OMP side effect. For every accepted artifact kind, feed the returned URI/digest back through the shared job/event validators and assert an altered kind, job ID, generation, digest, pluralization, or arbitrary `/jobs/` prefix fails. Add local command-poll cases proving it queries only `Jobs.OwnerCreatedAtIndex` for the authenticated owner, returns the stored `dispatch_command_id`, never exposes that internal ID in job reads or API responses, does not send SQS, returns no command on owner or state mismatch, and still returns an already-claimed job whose desired state changed to `CANCELLED` so the owner can record terminal proof without preparing a workspace. Add AWS cases proving the API handler never queries routing indexes or sends SQS, and that only the Router-owned helper does so after a successful conditional claim. No test may authorize from a body `agent_id`, `source`, or caller-supplied account/role.

- [ ] **Step 2: Confirm RED**

```bash
cd supervisor && npm test -- --test-name-pattern='cannot mutate|claim acceptance'
```

Expected: missing agent handler.

- [ ] **Step 3: Implement identity binding and event writes**

For local routes, use `event.requestContext.authorizer.lambda.agentId`. For AWS routes, read only `event.requestContext.authorizer.iam.userArn` and normalize it with `parseAwsAgentPrincipal(userArn, expectedAccountId, expectedRoleName)`. It must accept only `arn:${partition}:sts::${expectedAccountId}:assumed-role/${expectedRoleName}/${nonEmptySessionName}` with exactly one nonempty session segment, reject IAM role ARNs, a different account, role, service, or extra slash, and return the single configured AWS agent ID. Configure `AWS_AGENT_ACCOUNT_ID`, `AWS_AGENT_ROLE_NAME`, and `AWS_AGENT_ID` from the template; never compare an STS principal to an IAM role ARN and never treat `agent_id` or `source` from a request body as authority. When an event body carries the contract-required `source`, reject it unless it exactly equals this authenticated agent ID.

Validate the entire event contract before logging or persistence; do not maintain a second API-only field list. The event body contains only the contract keys, and `data` uses exactly the version-1 schema's flat allowlist and conditional branches. Reject nested `cleanup`, free-form `result`, raw prompt/source/model text, filesystem paths, tool payloads, arbitrary summary strings, and any field the shared schema rejects.

`POST .../transition` accepts exactly `{generation, from_state, to_state}` and only `CLAIMED→PREPARING` or `PREPARING→RUNNING`. It derives the owner from the authenticated principal, uses server time, validates the pair with the shared state-machine fixture, and performs one conditional Jobs update requiring that exact current state, owner, generation, and unexpired lease. It returns the shared-schema-valid updated job. It never accepts caller timestamps, arbitrary target states, terminal states, approval transitions, or a body agent ID.

`writeFencedEvent` uses the Task 3 two-item transaction shape containing the owner/generation/unexpired-lease condition, idempotent event put, and at most one Jobs state update. `terminal_status: "SUCCEEDED"` requires the schema's return code, provenance, and redacted-result artifact pairs and sets `SUCCEEDED`; `FAILED` requires `retryable: false`, allowlisted error code, stopped timestamp, and cleanup proof and sets `FAILED`; `CANCELLED` requires the flat stopped/cleanup proof and sets `CANCELLED`. A non-terminal `GATE_EVALUATED` event with exact `status_code: "WAITING_APPROVAL"` and `summary: "gate_evaluated"` updates `RUNNING→WAITING_APPROVAL` for every job and conditions on `approval_required=true`. An `ARTIFACT_UPDATED` event with exact `status_code:"PAUSED"`, `summary:"artifact_updated"`, and a verified checkpoint URI/digest updates `RUNNING→PAUSED`. Before that transaction, the handler retrieves and digest-validates the checkpoint, validates the private strict recovery-checkpoint shape, and derives metadata eligibility only when the native state is a safe restart boundary and every repo record has normalized identifiers, `clean:true`, `pushed:true`, `head == remote_commit`, and no source-bearing uncommitted state. It then stores the schema-valid public job checkpoint plus private `checkpoint_generation`, `recovery_origin_agent_id`, `recovery_origin_environment`, and `cross_node_eligible` attributes. This server derivation validates the authenticated checkpoint metadata; the claiming runtime must still fetch each remote ref and prove it resolves to the recorded commit before any workspace or OMP side effect. These private attributes never appear in `job-v1` responses and no caller-provided eligibility boolean is trusted. A `PROGRESS_UPDATE` event with exact `status_code:"RECOVERY_REQUIRED"` and `summary:"progress_update"` updates only `PREPARING|RUNNING|WAITING_APPROVAL→RECOVERY_REQUIRED` after stopped-process plus cleanup-refusal/unsafe-recovery evidence. Every branch derives current state server-side, validates the shared state-machine vector, and uses the same owner/generation/unexpired-lease condition; no arbitrary status string selects a state. These non-terminal events carry no terminal fields. No separate `result` object is accepted.

Job and prompt reads require the current owner and generation. Prompt retrieval derives `prompts/${job_id}.txt`, issues `HeadObject` and `GetObject`, requires `Metadata.sha256` to equal the computed SHA-256 of the UTF-8 bytes, and returns the text plus that digest without logging either. `GET .../checkpoint` is also current-owner/current-generation/current-lease fenced, but for a claim derived from `PAUSED` it requires the stored `checkpoint_generation == current generation - 1`; it reads only that stored checkpoint URI after re-validating its canonical bucket/key/prior-generation/digest, verifies the S3 bytes against the paired SHA-256, enforces the same 1 MiB bound, and returns exactly `{artifact_base64, sha256}`. It accepts no caller key or URI and never returns the private routing attributes. Artifact upload accepts only `checkpoint`, `provenance`, or `redacted-result`, verifies a decoded base64 body of at most 1 MiB before writing SSE-KMS, and writes respectively to `artifacts/checkpoints/${job_id}/${generation}/${sha256}.json`, `artifacts/provenance/${job_id}/${generation}/${sha256}.json`, or `artifacts/redacted-results/${job_id}/${generation}/${sha256}.json`. Its response contains only the canonical S3 URI and matching digest after both values pass the shared contract validators; never return a key shape that a job/event record cannot store.

- [ ] **Step 4: Implement command sources**

Local commands are read by querying the sparse `Jobs.OwnerCreatedAtIndex` with the authenticated `owner_agent_id`, ordered by `created_at`, and selecting that owner's `CLAIMED` rows regardless of current desired state. This is intentional: a claim cancelled before delivery must still reach its owner, whose executor re-reads the job and emits stopped/cleanup proof without preparing a workspace or starting OMP. Construct the command from the stored `dispatch_command_id`, job ID, generation, and literal type `EXECUTE`; never scan Jobs, route an unowned job, or create an ID during polling. AWS delivery uses the same fields returned by the conditional claim.

Local commands are returned by a bounded HTTP poll from the Jobs table. AWS commands are sent to SQS with:

```json
{
  "schema_version": 1,
  "command_id": "5a2c1e31-cf45-4fe4-ae47-f40d3eb90837",
  "job_id": "job-1",
  "generation": 4,
  "type": "EXECUTE"
}
```

Set SQS `MessageGroupId` only if a FIFO queue is selected. For the MVP, use a standard queue and rely on the agent command ledger.

- [ ] **Step 5: Run GREEN and commit**

```bash
cd supervisor && npm test
cd ..
git add supervisor/src/agent-handler.js supervisor/test/agent-handler.test.js
git commit -m "feat: add supervisor agent protocol"
```

### Task 7: Implement local-first routing and reuse the launcher lifecycle Lambda

**Files:**
- Create: `supervisor/src/lifecycle-client.js`
- Create: `supervisor/src/router.js`
- Test: `supervisor/test/lifecycle-client.test.js`
- Test: `supervisor/test/router.test.js`
- Modify: `launcher/src/handler.js`
- Modify: `launcher/test/handler.test.js`
- Modify: `supervisor/src/agent-handler.js`
- Modify: `supervisor/test/agent-handler.test.js`

- [ ] **Step 1: Write failing internal-lifecycle tests in the launcher package**

```js
test("accepts the IAM-only supervisor internal start event", async () => {
  const result = await handler({
    source: "awf.supervisor",
    version: 1,
    operation: "start",
    request_id: "5a2c1e31-cf45-4fe4-ae47-f40d3eb90837",
  });
  assert.equal(result.schema_version, 1);
  assert.equal(result.instance_state, "pending");
});

test("internal lifecycle event rejects extra job data", async () => {
  await assert.rejects(() => handler({
    source: "awf.supervisor",
    version: 1,
    operation: "start",
    request_id: "5a2c1e31-cf45-4fe4-ae47-f40d3eb90837",
    prompt: "must not cross lifecycle boundary",
  }), /unexpected field/);
});
```

The public API Gateway route tests must remain unchanged and passing.

- [ ] **Step 2: Write failing router tests**

Cover local healthy/capable/capacity, local stale, repo mismatch, explicit target, AWS healthy, AWS offline, convergent duplicate launcher starts, `BLOCKED`, claim expiry, stale running with and without checkpoint, no owner migration after `CLAIMED`, both safe PAUSED recovery branches, and the first AWS heartbeat handoff. PAUSED recovery first selects the healthy/capable prior exact agent when available and preserves the verified prior-generation checkpoint. If that agent is unavailable, a different agent in the same environment may claim only when server-derived `cross_node_eligible=true`; an alternate environment is the final choice only when that same flag is true and `requested_target` permits it. Cross-node recovery is allowed only for `desired_state=RUNNING`; `desired_state=CANCELLED` may be delivered only to the prior exact agent that can clean its retained workspace. Every PAUSED claim increments generation exactly once and sends one command. Reject cross-node claims for dirty/unpushed state, missing/mismatched origin metadata, false eligibility, ref/commit mismatch, explicit-target violation, or cancellation cleanup. For a normal initially offline AWS agent becoming `ONLINE`, the router conditionally claims the oldest eligible `QUEUED` job exactly once, then sends one `EXECUTE` command with that claimed generation. Concurrent start attempts may each send the same strict internal metadata-only payload and must converge on the same final launcher state; repeat the heartbeat and assert it emits neither a second claim nor a second SQS command.

- [ ] **Step 3: Confirm RED in both packages**

```bash
cd launcher && npm test
cd ../supervisor && npm test -- --test-name-pattern='routes|lifecycle'
```

Expected: internal lifecycle route and router exports are missing.

- [ ] **Step 4: Add strict internal dispatch to the existing launcher handler**

Dispatch internal events before HTTP payload handling only when the exact key set is present:

```js
const INTERNAL_KEYS = ["operation", "request_id", "source", "version"];

function isInternalLifecycleEvent(event) {
  return event?.source === "awf.supervisor" && event?.version === 1;
}
```

Validate `operation` as `status` or `start`. Reuse the same `instance-state.js` functions used by public routes. Do not add prompt, repo, session, or worker fields.

- [ ] **Step 5: Implement the Lambda invoke client and router**

```js
export async function requestInstanceStart({ lambda, functionName, requestId }) {
  const response = await lambda.send(new InvokeCommand({
    FunctionName: functionName,
    InvocationType: "RequestResponse",
    Payload: Buffer.from(JSON.stringify({
      source: "awf.supervisor",
      version: 1,
      operation: "start",
      request_id: requestId,
    })),
  }));
  return parseLifecyclePayload(response);
}
```

Export `routeAfterAwsHeartbeat({ agentId, now })` from `router.js`, but execute it only inside the Router Lambda. After the Agent Lambda durably records a valid AWS heartbeat that makes the configured agent `ONLINE`, it asynchronously invokes the Router Lambda with exact metadata-only payload `{source:"awf.supervisor.heartbeat",version:1,operation:"route-ready-agent",agent_id:AWS_AGENT_ID,request_id}` and returns success only after Lambda accepts that invocation. The Agent Lambda never queries routing indexes or sends SQS itself. The Router validates the exact key set and configured agent ID, runs the same reconciliation calculation, and queries `Jobs.StateCreatedAtIndex` in creation order. Normal routing considers only `QUEUED` jobs whose target is `auto` or the candidate environment, `desired_state=RUNNING`, capabilities/repos match, and capacity remains, with healthy local candidates before AWS. Recovery routing considers `PAUSED` jobs with a schema-valid stored prior-generation checkpoint and private server-derived origin/eligibility metadata. Rank the healthy/capable prior exact agent first; then, only for `desired_state=RUNNING` and `cross_node_eligible=true`, a compatible agent in the same environment; finally a compatible alternate environment when `requested_target` permits it. A cancelled PAUSED job routes only to the prior exact agent for retained-workspace cleanup. The conditional claim passes `recovery_origin_matches` and `commit_boundary_verified` evidence into the shared state-machine validator, preserves the checkpoint, increments generation once, and stores one UUID4 `dispatch_command_id`; only the claim winner sends the five-field `EXECUTE` command using that ID. If SQS send returns an error or its response is lost, leave that claim fenced until its short lease expires—never release a possibly consumed command. Reconciliation returns an expired, still-`CLAIMED`, never-started job to its source (`QUEUED` for normal work, `PAUSED` with checkpoint and private recovery metadata intact for recovery work) only with server-time lease and owner/generation conditions; a late command is then stale, while a command accepted before expiry renews the lease and defeats that recovery. Standard-queue duplicates of a successful send retain the same ID and are consumed once by the agent ledger.

Router ordering must match the approved design. After requesting EC2 start, keep the job `QUEUED`; the first valid AWS heartbeat triggers the narrow Router-Lambda handoff, which claims only after readiness is proven and uses the conditional generation increment as the idempotency fence. Tests must assert the Agent handler can query only the owner-delivery index and invokes only Router—never a routing index, SQS, or Launcher—while the Router-role test proves the first-heartbeat-to-command path.
The Router Lambda also accepts only the exact admin event `{source:"awf.supervisor.admin",version:1,operation:"wake-agent",agent_id:AWS_AGENT_ID,request_id:<UUID4>}` from the Admin Lambda. This operation calls the same `requestInstanceStart` lifecycle client and returns accepted lifecycle metadata without querying or mutating Jobs and without sending SQS. Reject every other source/operation/key/agent combination. Test duplicate wake events with the same request ID, malformed metadata, and that the Admin handler can invoke Router but cannot invoke Launcher.

- [ ] **Step 6: Run GREEN and commit**

```bash
cd launcher && npm test
cd ../supervisor && npm test
cd ..
git add launcher/src/handler.js launcher/test/handler.test.js supervisor/src/lifecycle-client.js supervisor/src/router.js supervisor/src/agent-handler.js supervisor/test/lifecycle-client.test.js supervisor/test/router.test.js supervisor/test/agent-handler.test.js
git commit -m "feat: route supervisor jobs through launcher lifecycle"
```

### Task 8: Provision the Control Plane with least privilege

**Files:**
- Modify: `infra/template.yaml`
- Modify: `scripts/deploy.sh`
- Modify: `.env.example`
- Test: `supervisor/test/template.test.js`
- Create: `tests/deploy_test.sh`

- [ ] **Step 1: Write failing template policy tests**

Use Python/YAML parsing rather than text grep. Assert:

- four DynamoDB tables: Jobs, Agents, Events, and `AccessTokens`; all use PITR and the customer-managed Supervisor KMS key. `AccessTokens` has hash key `access_token_hash`, TTL attribute `expires_at_epoch`, and contains no raw token. Jobs has `StateCreatedAtIndex` (`state`, `created_at`) for Router queries and sparse `OwnerCreatedAtIndex` (`owner_agent_id`, `created_at`) for authenticated local command delivery; the owner index uses `ProjectionType: INCLUDE` with exactly `state`, `desired_state`, `generation`, and `dispatch_command_id` as non-key attributes. Agents has `OnlineAgentsIndex` (`status`, `last_heartbeat_at`) for Router queries.
- one standard SQS command queue with `VisibilityTimeout: 120`, `MessageRetentionPeriod: 345600`, and `RedrivePolicy: { deadLetterTargetArn: !GetAtt CommandDlq.Arn, maxReceiveCount: 5 }`, plus a DLQ with `MessageRetentionPeriod: 1209600`
- one private S3 bucket with versioning, `aws:kms` default encryption bound to `SupervisorKey`, bucket keys, public-access block, TLS-only policy, and lifecycle rules: `prompts/` current/noncurrent expiry 30/7 days; `artifacts/redacted-results/` 90/30 days; `artifacts/checkpoints/` 365/90 days; `artifacts/provenance/` 365/90 days; and `bootstrap/` 90/7 days
- `SupervisorKey` with rotation enabled, `SupervisorKeyAlias` (`alias/awf-supervisor`), an account-root key-policy enablement for IAM policies, and an explicit key ARN binding in every bucket and DynamoDB SSE configuration
- admin, agent, local-authorizer, and router Lambda functions with distinct execution roles
- EventBridge reconciliation every minute
- HTTP API default-route throttle `ThrottlingRateLimit: 5` and `ThrottlingBurstLimit: 10`, inherited by the unauthenticated `POST /v1/local-agent/token` route; the local request authorizer uses payload format 2.0, simple responses, identity source `$request.header.Authorization`, and `ReauthorizeEvery: 0` so token revocation and 15-minute expiry are checked on every request
- admin routes use `AWS_IAM`
- every local-agent route, including `GET /v1/local-agent/commands`, uses the local authorizer
- AWS-agent routes use `AWS_IAM`
- the Agent Lambda can invoke only the Router Lambda for the metadata-only ready-agent handoff and can query only `Jobs.OwnerCreatedAtIndex` for authenticated local command delivery; the Admin Lambda can invoke only the Router Lambda for metadata-only cleanup wake; neither can invoke the launcher, and Agent has no routing-index Query or SQS permission
- Supervisor router can invoke only the launcher integration Lambda
- Supervisor functions have no `ec2:StartInstances`
- EC2 role can consume only the AWS command queue and call only AWS-agent routes

- [ ] **Step 2: Confirm RED**

```bash
cd supervisor && npm test -- --test-name-pattern='template'
```

Expected: missing Supervisor resources.

- [ ] **Step 3: Add exact SAM resources**

Use `AWS::Serverless::HttpApi` with route-level authorizers and the explicit default throttle above. Configure `LocalAuthorizer` with `AuthorizerPayloadFormatVersion: "2.0"`, `EnableSimpleResponses: true`, `Identity: { Headers: [Authorization], ReauthorizeEvery: 0 }`; do not cache bearer authorization results. Set Lambda environment variables to resource names and ARNs, including `SUPERVISOR_KEY_ARN`, `AWS_AGENT_ACCOUNT_ID`, `AWS_AGENT_ROLE_NAME`, and `AWS_AGENT_ID`. Configure reserved concurrency for router and authorizer. Define `SupervisorKey` and `SupervisorKeyAlias`, bind `!GetAtt SupervisorKey.Arn` as the S3 `KMSMasterKeyID` and as `KMSMasterKeyId` for Jobs, Agents, Events, and AccessTokens, and set `BucketKeyEnabled: true`.

Attach distinct inline data-plane statements, with no `Resource: "*"` on `dynamodb:*`, `s3:*`, `sqs:*`, or `kms:*` actions:

| Execution role | Exact allowed data-plane actions and resources |
| --- | --- |
| Admin | `dynamodb:GetItem,PutItem,UpdateItem,TransactWriteItems` on Jobs, Agents, Events, and AccessTokens; `dynamodb:Scan` on Agents only; `lambda:InvokeFunction` only on `!GetAtt SupervisorRouterFunction.Arn`; `s3:PutObject` on `${ArtifactsBucket}/prompts/*`; `kms:GenerateDataKey` on SupervisorKey only with `kms:ViaService = s3.${AWS::Region}.amazonaws.com` and `kms:EncryptionContext:aws:s3:arn = arn:${AWS::Partition}:s3:::${ArtifactsBucket}/prompts/*`. This covers submit, immutable approval decisions, token compare-and-swap, revocation, and metadata-only cleanup wake without Scan on tokens/events. |
| Agent | `dynamodb:GetItem,UpdateItem,TransactWriteItems` on Jobs, Agents, and Events; `dynamodb:Query` only on `Jobs/OwnerCreatedAtIndex`; `lambda:InvokeFunction` only on `!GetAtt SupervisorRouterFunction.Arn`; `s3:GetObject` only on `${ArtifactsBucket}/prompts/*` and `${ArtifactsBucket}/artifacts/checkpoints/*`; `s3:PutObject` on `${ArtifactsBucket}/artifacts/*`; `kms:Decrypt,GenerateDataKey` on SupervisorKey only through that S3 service and with encryption-context values limited to the prompt and artifact prefixes. It has no GetObject access to provenance/redacted-results, StateCreatedAt/OnlineAgents index Query, Scan, SQS, launcher, or EC2 permission. |
| Local authorizer | `dynamodb:GetItem` on AccessTokens only. It has no wildcard, Scan, Query, S3, SQS, KMS, Lambda, EC2, or router permission. |
| Router | `dynamodb:Query` on `Jobs/StateCreatedAtIndex` and `Agents/OnlineAgentsIndex`; `dynamodb:GetItem,UpdateItem` on Jobs and Agents only; `sqs:SendMessage` on CommandQueue only; and `lambda:InvokeFunction` only on `!GetAtt LauncherIntegrationFunction.Arn`. It has no S3, KMS, EC2, or event-write permission. |
| EC2 agent role | `sqs:ReceiveMessage,DeleteMessage,ChangeMessageVisibility,GetQueueAttributes` on CommandQueue; `execute-api:Invoke` only on the deployed `/v1/aws-agent/*` ARN; `s3:GetObject,GetObjectVersion` only on `${ArtifactsBucket}/bootstrap/*`; and `kms:Decrypt` on SupervisorKey only via S3 with `kms:EncryptionContext:aws:s3:arn = arn:${AWS::Partition}:s3:::${ArtifactsBucket}/bootstrap/*`. It receives no direct job-artifact access, DynamoDB, admin-route, or EC2 lifecycle permission. |

The parsed-template test must assert every action/resource pair, each KMS key binding and condition, all three index definitions, the queue-to-DLQ ARN linkage and receive count, each lifecycle ID/prefix/retention pair, and absence of wildcard data-plane resources. It must specifically prove that the Agent and Admin Lambdas may invoke Router only for their exact metadata handoffs but may not invoke Launcher, that Agent may query only `Jobs/OwnerCreatedAtIndex` and may not query either routing index or send SQS, and that Router may invoke only Launcher. It must also assert every local route uses the uncached payload-v2 `LocalAuthorizer` with Authorization identity source, `POST /v1/local-agent/token` inherits the 5/10 stage throttle, and the configured AWS IAM route set is exactly `/v1/aws-agent/*`.

Do not give any Supervisor function EC2 lifecycle permissions. Add exactly these outputs: `SupervisorApiUrl` (the HTTPS HTTP API base URL), `AwsCommandQueueUrl` (CommandQueue URL), `SupervisorArtifactsBucketName` (bucket name), and `LocalAuthorizerFunctionArn`; do not output KMS key material or enrollment tokens.

- [ ] **Step 4: Update deployment inputs and packaging**

`scripts/deploy.sh` must run the release gates before it builds or deploys:

```bash
(cd "$PROJECT_DIR/launcher" && npm ci && npm test)
(cd "$PROJECT_DIR/supervisor" && npm ci && npm test)
sam validate --lint --template "$PROJECT_DIR/infra/template.yaml"
sam build --template-file "$PROJECT_DIR/infra/template.yaml"
sam deploy \
  --profile "$AGENT_AWS_PROFILE" \
  --region "$AGENT_AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --capabilities CAPABILITY_IAM \
  --resolve-s3 \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    UbuntuAmiId="$ubuntu_ami" \
    SubnetId="$AGENT_SUBNET_ID" \
    VpcId="$subnet_vpc_id" \
    AvailabilityZone="$subnet_az" \
    InstanceType="$AGENT_INSTANCE_TYPE" \
    VolumeSize="$AGENT_VOLUME_SIZE" \
    IdleStopMinutes="$AGENT_IDLE_STOP_MINUTES"
```

Create `tests/deploy_test.sh` with temporary fake `npm` and `sam` executables that append each invocation to a log. In separate cases, force the launcher test, Supervisor test, and `sam validate --lint` to fail; assert the script returns that failure and the log contains neither `sam build` nor `sam deploy`. In its passing case, assert the exact ordered gates `launcher npm ci`, `launcher npm test`, `supervisor npm ci`, `supervisor npm test`, `sam validate --lint --template ...`, then `sam build`, then `sam deploy`.

- [ ] **Step 5: Validate and run GREEN**

```bash
(cd supervisor && npm ci && npm test)
(cd launcher && npm ci && npm test)
npm --prefix supervisor test -- --test-name-pattern='template'
bash tests/deploy_test.sh
sam validate --lint --template infra/template.yaml
```

Expected: all tests pass and SAM lint succeeds.

- [ ] **Step 6: Commit**

```bash
git add infra/template.yaml scripts/deploy.sh .env.example supervisor/test/template.test.js tests/deploy_test.sh
git commit -m "feat: provision supervisor control plane"
```

### Task 9: Verify the packaged Control Plane locally

**Files:**
- Create: `tests/supervisor_api_integration_test.mjs`
- Create: `tests/run-supervisor-local.sh`

- [ ] **Step 1: Add an integration harness with local fake AWS clients**

The test must execute real handlers and domain/store code while fake clients capture DynamoDB, S3, SQS, and Lambda calls. It must run this scenario:

```text
submit (server adds approval_required=true) -> local heartbeat -> router claim -> claim acceptance -> PREPARING
-> RUNNING -> GATE_EVALUATED -> WAITING_APPROVAL -> APPROVE/CONTINUE -> RUNNING -> SUCCEEDED
```

Run a second scenario with local offline:

```text
submit -> router internal launcher start -> valid AWS assumed-role heartbeat -> conditional AWS claim -> one AWS queue command
```

Also force the terminal-event handoff race and assert no stale Events item is visible; reject a submit body containing `approval_required:false`; submit an event with prompt/raw-model/source-code fields and assert it is rejected before a log or write; exercise issue/immediate lookup/expiry/revoke of an opaque local token; and assert prompt retrieval derives `prompts/${job_id}.txt` and verifies only S3 metadata. Assert no second owner, no EC2 permission in Supervisor calls, and a stale generation event returns 409.

- [ ] **Step 2: Confirm the harness fails before fixture wiring**

```bash
node --test tests/supervisor_api_integration_test.mjs
```

Expected: fixture dependencies are not yet provided.

- [ ] **Step 3: Wire deterministic fake clients and complete the scenarios**

Use fixed server times and UUIDs. Do not call AWS. The harness must assert complete request and response bodies, not only status codes.

- [ ] **Step 4: Run all local verification**

```bash
npm --prefix supervisor test
npm --prefix launcher test
node --test tests/supervisor_api_integration_test.mjs
npm --prefix supervisor test -- --test-name-pattern='template'
sam validate --lint --template infra/template.yaml
```

Expected: every command succeeds.

- [ ] **Step 5: Commit**

```bash
git add tests/supervisor_api_integration_test.mjs tests/run-supervisor-local.sh
git commit -m "test: cover supervisor control plane flow"
```
