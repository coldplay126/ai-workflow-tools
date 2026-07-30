# AWF Supervisor AWS Host Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install the tested AWF Supervisor Agent on the persistent EC2 workspace, connect it to the Control Plane through the instance role and SQS, and prevent automatic stop while a lease or outbox is active.

**Architecture:** The `awf-cli` wheel is built locally from a pinned source commit, uploaded to the private Supervisor artifact bucket, and installed through SSM. A systemd service runs as `ubuntu`, uses `/workspace` for durable state, and uses an active-lease marker under `/var/lib/aws-agent` for local shutdown gating. The existing idle-stop script asks AWF for a fail-closed stop decision before calling `shutdown`.

**Tech Stack:** Bash, systemd, AWS SSM, S3, EC2 instance role, SQS, `uv tool`, `agentctl`, shell fixture tests

---

## Preconditions

- Complete the AWF agent-runtime plan and record the exact Git commit in `AWF_SUPERVISOR_SOURCE_REF`.
- Complete the AWS Control Plane plan and deploy its SAM stack.
- Complete the Cloudflare OMP Remote Access plan first; preserve its gateway, launcher, tunnel, and readiness services.
- Work in an isolated `aws-agent-poc` worktree.
- No script may copy `.env`, OAuth state, GitHub tokens, or model credentials into S3 or command output.

## File map

- `infra/install-supervisor-agent.sh`: root-run idempotent installer for wheel, state dirs, env, and systemd.
- `infra/aws-agent-idle-stop`: source-controlled idle-stop implementation.
- `scripts/update-supervisor-agent.sh`: build, checksum, upload, and SSM rollout.
- `scripts/status-supervisor-agent.sh`: redacted service and Control Plane health.
- `infra/template.yaml`: instance-role permissions and bootstrap prerequisites.
- `tests/install-supervisor-agent_test.sh`: filesystem/systemd fixture test.
- `tests/aws-agent-idle-stop_test.sh`: automatic-stop safety test.
- `tests/update-supervisor-agent_test.sh`: mocked AWS CLI rollout test.

### Task 1: Extract and harden the idle-stop program

**Files:**
- Create: `infra/aws-agent-idle-stop`
- Modify: `infra/template.yaml` in the current `aws-agent-idle-stop` here-document
- Create: `tests/aws-agent-idle-stop_test.sh`

- [ ] **Step 1: Write failing fail-closed idle tests**

The fixture must replace `pgrep`, `logger`, and `shutdown` through `PATH`. It must create the fake AWF executable at `$TEST_ROOT/opt/awf/current/bin/awf` and invoke the program with `AWF_SUPERVISOR_AWF_BIN="$TEST_ROOT/opt/awf/current/bin/awf"`; production never discovers AWF through `PATH`. The fake records its arguments and returns busy when the path supplied by `--active-lease-path` exists. Cover:

```text
idle disabled -> no shutdown
.keep-awake exists -> no shutdown
awf idle-status exit 3 busy -> no shutdown
awf idle-status exit 4 unknown -> no shutdown
absolute awf binary missing or non-executable -> no shutdown
agent-created canonical active-lease marker -> idle-status sees that exact path and no shutdown
outbox busy -> no shutdown
OMP/provider process running -> refresh activity, no shutdown
safe status + threshold not reached -> no shutdown
safe status + threshold reached -> exactly one shutdown -h now
```

```bash
run_case "busy lease" 3 7200
[[ ! -e "$TEST_ROOT/shutdown-called" ]] || fail "busy lease must block automatic stop"
grep -Fx -- '--environment' "$TEST_ROOT/awf-argv" >/dev/null
grep -Fx -- '--active-lease-path' "$TEST_ROOT/awf-argv" >/dev/null
grep -Fx "$TEST_ROOT/var/lib/aws-agent/supervisor-active-lease.json" "$TEST_ROOT/awf-argv" >/dev/null
```


- [ ] **Step 2: Confirm RED**

```bash
bash tests/aws-agent-idle-stop_test.sh
```

Expected: the source-controlled program does not exist.

- [ ] **Step 3: Implement a fail-closed stop gate**

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

idle_minutes="$(cat "${AWF_IDLE_MINUTES_FILE:-/etc/aws-agent-idle-minutes}")"
workspace="${AWS_AGENT_WORKSPACE:-/workspace}"
state_dir="${AWF_SUPERVISOR_STATE_DIR:-/workspace/.awf-supervisor}"
lease_path="${AWF_SUPERVISOR_ACTIVE_LEASE_PATH:-/var/lib/aws-agent/supervisor-active-lease.json}"
awf_bin="${AWF_SUPERVISOR_AWF_BIN:-/opt/awf/current/bin/awf}"
last_active="${AWS_AGENT_LAST_ACTIVE_FILE:-/var/lib/aws-agent/last-active}"

if [[ "$idle_minutes" == "0" || -e "$workspace/.keep-awake" ]]; then
  exit 0
fi

if [[ "$awf_bin" != /* || ! -x "$awf_bin" ]]; then
  logger -t aws-agent-poc "Automatic stop blocked: AWF is unavailable."
  exit 0
fi

set +e
AWF_SUPERVISOR_ACTIVE_LEASE_PATH="$lease_path" \
  "$awf_bin" supervisor agent idle-status \
    --environment aws \
    --state-dir "$state_dir" \
    --active-lease-path "$lease_path" \
    >/dev/null 2>&1
idle_rc=$?
set -e
if [[ "$idle_rc" -ne 0 ]]; then
  logger -t aws-agent-poc "Automatic stop blocked: supervisor idle status rc=$idle_rc."
  exit 0
fi
```

Keep the existing provider-process check and idle timestamp calculation after this gate. Only exact exit 0 permits the timeout check to call `shutdown`.

- [ ] **Step 4: Provision the gate prerequisites and one timer integration**

Delete the duplicated idle-stop here-document from `infra/template.yaml`; `infra/aws-agent-idle-stop` is the sole implementation. In cloud-init, install the AWS CLI and Python venv support before any SSM rollout can call the installer:

```bash
apt-get install -y awscli python3-venv
/usr/bin/python3 -m venv --help >/dev/null
/usr/bin/python3 --version
/usr/bin/aws --version
```

Cloud-init must write only the timer and service, never a second copy of the script:

```ini
# /etc/systemd/system/aws-agent-idle-stop.service
[Unit]
Description=Stop AWS agent EC2 after idle timeout
ConditionPathIsExecutable=/usr/local/sbin/aws-agent-idle-stop

[Service]
Type=oneshot
EnvironmentFile=-/etc/awf-supervisor-agent.env
ExecStart=/usr/local/sbin/aws-agent-idle-stop
```

```ini
# /etc/systemd/system/aws-agent-idle-stop.timer
[Unit]
Description=Check AWS agent EC2 idle timeout

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
Unit=aws-agent-idle-stop.service

[Install]
WantedBy=timers.target
```

Run `systemctl daemon-reload` followed by `systemctl enable --now aws-agent-idle-stop.timer` in cloud-init. The condition keeps a new host fail-closed until Task 2 installs the first verified script. The installer must install every later version at `/usr/local/sbin/aws-agent-idle-stop`, then run `systemctl daemon-reload` and `systemctl restart aws-agent-idle-stop.timer`; the rollout fixture must assert those exact actions and the `aws`/`uv` prerequisite checks.

- [ ] **Step 5: Run GREEN and SAM validation**

```bash
bash tests/aws-agent-idle-stop_test.sh
sam validate --lint --template infra/template.yaml
```

Expected: all cases pass and the template validates.

- [ ] **Step 6: Commit**

```bash
git add infra/aws-agent-idle-stop infra/template.yaml tests/aws-agent-idle-stop_test.sh
git commit -m "fix: block EC2 auto-stop for supervisor work"
```

### Task 2: Build an idempotent systemd installer

**Files:**
- Create: `infra/install-supervisor-agent.sh`
- Create: `tests/install-supervisor-agent_test.sh`

- [ ] **Step 1: Write the failing root-prefix fixture test**

The installer accepts `ROOT_PREFIX` for tests and these required inputs:

```text
AWF_WHEEL_PATH
AWF_WHEEL_SHA256
AWF_IDLE_STOP_PATH
AWF_IDLE_STOP_SHA256
AWF_SUPERVISOR_API_URL
AWF_SUPERVISOR_AWS_QUEUE_URL
AWF_SUPERVISOR_REGION
AWF_SUPERVISOR_AGENT_ID
```

Assert it creates:

```text
/opt/awf/releases/$AWF_WHEEL_SHA256/                         root:root 0755
/opt/awf/current -> releases/$AWF_WHEEL_SHA256                atomic symlink
/opt/awf/idle-stop-releases/$AWF_IDLE_STOP_SHA256/aws-agent-idle-stop  root:root 0755
/usr/local/sbin/aws-agent-idle-stop -> ../../../opt/awf/idle-stop-releases/$AWF_IDLE_STOP_SHA256/aws-agent-idle-stop  atomic symlink
/workspace/.awf-supervisor                                    ubuntu:ubuntu 0700
/workspace/repos                                              ubuntu:ubuntu 0755
/workspace/tasks                                              ubuntu:ubuntu 0700
/workspace/worktrees                                          ubuntu:ubuntu 0700
/var/lib/aws-agent                                             root:ubuntu 0770
/var/lib/aws-agent/supervisor-active-lease.json                absent initially
/etc/awf-supervisor-agent.env                                  root:root 0600
/etc/systemd/system/awf-supervisor-agent.service               root:root 0644
```

Run an atomic create-and-replace of the lease marker as `ubuntu` and assert it succeeds while the marker remains unreadable to non-root, non-`ubuntu` users. Assert a checksum or candidate-doctor failure leaves both live symlinks and all live configuration unchanged. Before a configuration-changing rollout, seed different prior environment/unit contents and active/enabled state; assert a failed post-restart doctor atomically restores both prior symlink targets, the exact prior environment and unit bytes/modes/owners, and the prior service state before restarting the previous service. Also cover the first-install post-restart failure: both symlinks, the newly created environment/unit files, and any enabled service are absent/stopped, systemd is reloaded, and the installer exits nonzero.

The test runs the installer twice and requires identical file content on the second run.

- [ ] **Step 2: Confirm RED**

```bash
bash tests/install-supervisor-agent_test.sh
```

Expected: installer missing.

- [ ] **Step 3: Verify and stage the exact release contents without replacing a live release**

Require every digest to be lowercase 64-hex and verify the downloaded wheel and idle-stop source before staging:

```bash
verify_sha256() {
  local path="$1" expected="$2" actual
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || { echo "Invalid SHA-256." >&2; exit 1; }
  actual="$(sha256sum "$path" | cut -d' ' -f1)"
  [[ "$actual" == "$expected" ]] || { echo "Checksum mismatch for $path." >&2; exit 1; }
}
verify_sha256 "$AWF_WHEEL_PATH" "$AWF_WHEEL_SHA256"
verify_sha256 "$AWF_IDLE_STOP_PATH" "$AWF_IDLE_STOP_SHA256"
```

Before creating the virtual environment, require the wheel to contain exactly this five-entry Supervisor contract manifest (the wheel may contain unrelated package files):

```bash
state_dir="$root/workspace/.awf-supervisor"
install -d -o ubuntu -g ubuntu -m 0700 "$state_dir"
install -d -o ubuntu -g ubuntu -m 0755 "$root/workspace/repos"
install -d -o ubuntu -g ubuntu -m 0700 "$root/workspace/tasks" "$root/workspace/worktrees"
contract_manifest="$state_dir/contract-manifest.json"
python3 - "$AWF_WHEEL_PATH" "$contract_manifest" <<'PY'
import hashlib, json, sys, zipfile

required = {
    "agent-v1.json": "awf/supervisor/schemas/agent-v1.json",
    "command-v1.json": "awf/supervisor/schemas/command-v1.json",
    "event-v1.json": "awf/supervisor/schemas/event-v1.json",
    "job-v1.json": "awf/supervisor/schemas/job-v1.json",
    "state-machine-v1.json": "awf/supervisor/fixtures/state-machine-v1.json",
}
with zipfile.ZipFile(sys.argv[1]) as wheel:
    names = set(wheel.namelist())
    missing = [path for path in required.values() if path not in names]
    if missing:
        raise SystemExit(f"wheel missing Supervisor contract entries: {', '.join(missing)}")
    manifest = {
        "schema_version": 1,
        "files": {
            name: hashlib.sha256(wheel.read(path)).hexdigest()
            for name, path in sorted(required.items())
        },
    }
with open(sys.argv[2], "w", encoding="utf-8") as output:
    json.dump(manifest, output, indent=2, sort_keys=True)
    output.write("\n")
PY
chown ubuntu:ubuntu "$contract_manifest"
chmod 0600 "$contract_manifest"
contract_manifest_sha256="$(sha256sum "$contract_manifest" | cut -d' ' -f1)"
```

Create `/var/lib/aws-agent` with `install -d -o root -g ubuntu -m 0770` before the candidate doctor so `ubuntu` can atomically manage the canonical `/var/lib/aws-agent/supervisor-active-lease.json` while root retains access. Stage the wheel only when `/opt/awf/releases/$AWF_WHEEL_SHA256/bin/awf` is absent:

```bash
release_root="$root/opt/awf/releases"
mkdir -p "$release_root"
release_dir="$release_root/$AWF_WHEEL_SHA256"
if [[ ! -x "$release_dir/bin/awf" ]]; then
  staged="$(mktemp -d "$release_root/.staged-$AWF_WHEEL_SHA256.XXXXXX")"
  trap 'rm -rf "$staged"' EXIT
  /usr/bin/python3 -m venv "$staged"
  "$staged/bin/python" -m pip install --disable-pip-version-check "$AWF_WHEEL_PATH"
  mv "$staged" "$release_dir"
  trap - EXIT
fi
```

Stage the verified idle-stop source at `/opt/awf/idle-stop-releases/$AWF_IDLE_STOP_SHA256/aws-agent-idle-stop` with `install -o root -g root -m 0755`; do not yet replace its live symlink. Candidate doctor must run as `ubuntu` with every service setting, not defaults:

```bash
sudo -u ubuntu env \
  AWF_SUPERVISOR_API_URL="$AWF_SUPERVISOR_API_URL" \
  AWF_SUPERVISOR_AWS_QUEUE_URL="$AWF_SUPERVISOR_AWS_QUEUE_URL" \
  AWF_SUPERVISOR_REGION="$AWF_SUPERVISOR_REGION" \
  AWF_SUPERVISOR_AGENT_ID="$AWF_SUPERVISOR_AGENT_ID" \
  AWF_SUPERVISOR_STATE_DIR="$state_dir" \
  AWF_SUPERVISOR_ACTIVE_LEASE_PATH="$root/var/lib/aws-agent/supervisor-active-lease.json" \
  AWS_AGENT_WORKSPACE="$root/workspace" \
  AWF_SUPERVISOR_REPO_ROOT="$root/workspace/repos" \
  "$release_dir/bin/awf" supervisor agent doctor \
    --agent-id "$AWF_SUPERVISOR_AGENT_ID" \
    --environment aws \
    --state-dir "$state_dir" \
    --active-lease-path "$root/var/lib/aws-agent/supervisor-active-lease.json" \
    --repo-root "$root/workspace/repos" \
    --json
```

After a successful rollout, atomically replace `$state_dir/installed.json` as `ubuntu:ubuntu` mode `0600` with the installed AWF version, wheel SHA-256, idle-stop SHA-256, and the complete `contract_manifest` plus `contract_manifest_sha256`. Never mutate either live symlink before checksum, five-entry manifest validation, package installation, and candidate doctor all pass.

- [ ] **Step 4: Render the exact environment and units**

Reject line breaks and shell metacharacters in the API URL, queue URL, region, and agent ID before rendering the root-only environment file. Render only validated `KEY=value` lines:

```text
AWF_SUPERVISOR_API_URL=${resolved_api_url}
AWF_SUPERVISOR_AWS_QUEUE_URL=${resolved_queue_url}
AWF_SUPERVISOR_REGION=ap-northeast-2
AWF_SUPERVISOR_AGENT_ID=${configured_agent_id}
AWF_SUPERVISOR_STATE_DIR=/workspace/.awf-supervisor
AWF_SUPERVISOR_ACTIVE_LEASE_PATH=/var/lib/aws-agent/supervisor-active-lease.json
AWF_SUPERVISOR_REPO_ROOT=/workspace/repos
AWS_AGENT_WORKSPACE=/workspace
```

```ini
[Unit]
Description=AWF Supervisor AWS Agent
After=network-online.target amazon-ssm-agent.service
Wants=network-online.target
ConditionPathIsMountPoint=/workspace

[Service]
Type=simple
User=ubuntu
Group=ubuntu
EnvironmentFile=/etc/awf-supervisor-agent.env
ExecStartPre=/opt/awf/current/bin/awf supervisor agent doctor --agent-id ${AWF_SUPERVISOR_AGENT_ID} --environment aws --state-dir ${AWF_SUPERVISOR_STATE_DIR} --active-lease-path ${AWF_SUPERVISOR_ACTIVE_LEASE_PATH} --repo-root ${AWF_SUPERVISOR_REPO_ROOT}
ExecStart=/opt/awf/current/bin/awf supervisor agent run --agent-id ${AWF_SUPERVISOR_AGENT_ID} --environment aws --transport sqs --repo-root ${AWF_SUPERVISOR_REPO_ROOT}
Restart=on-failure
RestartSec=10
TimeoutStopSec=90
KillMode=control-group
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/workspace/.awf-supervisor /workspace/repos /workspace/tasks /workspace/worktrees /var/lib/aws-agent

[Install]
WantedBy=multi-user.target
```

`AWF_SUPERVISOR_ACTIVE_LEASE_PATH` and `AWF_SUPERVISOR_REPO_ROOT` are single path contracts: they are supplied to the agent; the lease path is also passed explicitly by the idle-stop program; and both are loaded from the same root-only environment file. Heartbeat discovery, doctor, and the workspace adapter use only that repository root.

- [ ] **Step 5: Enable only after doctor passes, with complete rollback**

Before writing any live file, create a root-owned mode-`0700` rollback directory on the same filesystem. Record whether `/opt/awf/current`, `/usr/local/sbin/aws-agent-idle-stop`, `/etc/awf-supervisor-agent.env`, and `/etc/systemd/system/awf-supervisor-agent.service` exist; save exact symlink targets and byte-for-byte file copies with original owner/group/mode; and record whether the service is enabled and active. Never print or upload the environment backup. Replace config files with same-directory temporary files plus `mv`, and replace a live symlink only through a sibling temporary symlink and `mv -Tf`, using `releases/$AWF_WHEEL_SHA256` for `current` and `../../../opt/awf/idle-stop-releases/$AWF_IDLE_STOP_SHA256/aws-agent-idle-stop` for the idle-stop link.

After candidate doctor passes, atomically install the rendered environment and unit, atomically replace `/opt/awf/current`, run `systemctl daemon-reload`, enable and restart `awf-supervisor-agent.service`, and run a post-restart doctor with the complete rendered environment. Only then atomically replace `/usr/local/sbin/aws-agent-idle-stop`, run `systemctl daemon-reload`, and `systemctl restart aws-agent-idle-stop.timer`.

On any failure after a live config file or symlink changes, stop the candidate service; atomically restore or remove each environment/unit file and symlink according to the snapshot; restore file metadata; run `systemctl daemon-reload`; restore the exact prior enabled/disabled and active/inactive state; and only then restart the prior active service and run its doctor. For an initial install, remove both links and both new config files, disable/stop the unit, reload systemd, and return nonzero. Delete the private rollback directory on both success and completed rollback. Keep the current and immediately previous wheel and idle-stop release directories; delete neither until a later successful rollout.

- [ ] **Step 6: Run GREEN and commit**

```bash
bash tests/install-supervisor-agent_test.sh
git add infra/install-supervisor-agent.sh tests/install-supervisor-agent_test.sh
git commit -m "feat: install supervisor EC2 service"
```

### Task 3: Grant only the AWS agent's required permissions

**Files:**
- Modify: `infra/template.yaml`
- Modify: `supervisor/test/template.test.js`

- [ ] **Step 1: Extend failing IAM assertions**

Assert the EC2 role can:

- `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:ChangeMessageVisibility`, `sqs:GetQueueAttributes` on the AWS command queue only
- `execute-api:Invoke` on `/v1/aws-agent/*` only
- `s3:GetObject` and `s3:GetObjectVersion` only on the Supervisor bucket's `bootstrap/*` prefix
- `kms:Decrypt` only through S3 with encryption context bound to that exact bootstrap prefix
- no direct artifact S3 write/read; prompt and artifact traffic goes through the authenticated Supervisor API

Assert it cannot:

- invoke admin or local-agent routes
- start or stop EC2
- read launcher or Cloudflare secrets
- scan all S3 buckets
- mutate DynamoDB directly when the API owns lease writes

- [ ] **Step 2: Confirm RED**

```bash
npm --prefix supervisor test -- --test-name-pattern='template'
```

Expected: required instance-role statements are absent.

- [ ] **Step 3: Add scoped statements**

Prefer the Supervisor API for heartbeat, lease, and event writes. Grant no DynamoDB write to EC2. SQS and bootstrap S3 are the only direct data-plane permissions required by the AWS agent.

- [ ] **Step 4: Run GREEN and commit**

```bash
npm --prefix supervisor test -- --test-name-pattern='template'
sam validate --lint --template infra/template.yaml
git add infra/template.yaml supervisor/test/template.test.js
git commit -m "feat: authorize supervisor EC2 agent"
```

### Task 4: Build and roll out a pinned AWF wheel through SSM

**Files:**
- Create: `scripts/update-supervisor-agent.sh`
- Create: `tests/update-supervisor-agent_test.sh`
- Modify: `.env.example`

- [ ] **Step 1: Write the failing mocked-AWS rollout test**

Fake `git`, `uv`, `aws`, `sha256sum`, `python3`, `systemctl`, and `sudo`. Assert the script:

1. verifies `AWF_SUPERVISOR_SOURCE_REF` exists and is 40 lowercase hex
2. builds from a clean detached archive/worktree at that exact ref
3. rejects a wheel missing any one of `agent-v1.json`, `command-v1.json`, `event-v1.json`, `job-v1.json`, or `state-machine-v1.json`
4. calculates SHA-256 for the wheel, installer, and idle-stop source, uploads all three below `bootstrap/$AWF_SUPERVISOR_SOURCE_REF/`, and captures a nonempty S3 `VersionId` for each `put-object`
5. resolves `SupervisorArtifactsBucketName`, `SupervisorApiUrl`, `AwsCommandQueueUrl`, and `InstanceId` from stack outputs
6. sends exactly two bounded SSM commands: an immutable installer command containing only the three object keys, their three `VersionId` values, their three SHA-256 values, API URL, queue URL, region, and agent ID; then the post-restart doctor command—neither contains credentials
7. makes the installer command preflight `/usr/bin/aws` and `/usr/bin/python3` venv support, downloads each object with `aws s3api get-object --version-id`, verifies its SHA-256 before use, and passes the verified idle-stop path and SHA to the installer
8. sources `/etc/awf-supervisor-agent.env` only as the root SSM shell and passes every configured service variable plus `--environment aws`, `--state-dir`, `--active-lease-path`, and `--repo-root` to `sudo -u ubuntu env /opt/awf/current/bin/awf supervisor agent doctor`
9. JSON-parses both invocation statuses as `Success` and removes the temporary local build directory

- [ ] **Step 2: Confirm RED**

```bash
bash tests/update-supervisor-agent_test.sh
```

Expected: rollout script missing.

- [ ] **Step 3: Implement reproducible build, immutable upload, and bounded install**

Required environment:

```bash
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AWF_SOURCE_DIR=/Users/steven/Documents/GitHub/ai-workflow-tools
AWF_SUPERVISOR_SOURCE_REF="$(git -C "$AWF_SOURCE_DIR" rev-parse HEAD)"
: "${AGENT_AWS_PROFILE:?Export AGENT_AWS_PROFILE with the existing SSO profile name}"
AGENT_AWS_REGION=ap-northeast-2
STACK_NAME=aws-agent-poc
```

Resolve the exact stack outputs with:

```bash
stack_output() {
  local key="$1" value
  value="$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --profile "$AGENT_AWS_PROFILE" \
    --region "$AGENT_AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue | [0]" \
    --output text)"
  [[ -n "$value" && "$value" != "None" ]] || {
    echo "Missing required stack output: $key" >&2
    exit 1
  }
  printf '%s\n' "$value"
}
bucket="$(stack_output SupervisorArtifactsBucketName)"
api_url="$(stack_output SupervisorApiUrl)"
queue_url="$(stack_output AwsCommandQueueUrl)"
instance_id="$(stack_output InstanceId)"
```

Reject a non-40-lowercase-hex ref. Create a temporary detached worktree, build exactly one wheel, and require all five contract artifacts before calculating immutable digests:

```bash
shopt -s nullglob
wheels=("$build_dir"/cli/dist/*.whl)
(( ${#wheels[@]} == 1 )) || {
  echo "Expected exactly one wheel." >&2
  exit 1
}
wheel="${wheels[0]}"
python3 - "$wheel" <<'PY'
import sys, zipfile
required = {
    "awf/supervisor/schemas/agent-v1.json",
    "awf/supervisor/schemas/command-v1.json",
    "awf/supervisor/schemas/event-v1.json",
    "awf/supervisor/schemas/job-v1.json",
    "awf/supervisor/fixtures/state-machine-v1.json",
}
with zipfile.ZipFile(sys.argv[1]) as archive:
    missing = sorted(required - set(archive.namelist()))
if missing:
    raise SystemExit(f"wheel missing Supervisor contract entries: {', '.join(missing)}")
PY
installer_path="$repo_root/infra/install-supervisor-agent.sh"
idle_stop_path="$repo_root/infra/aws-agent-idle-stop"
wheel_sha256="$(shasum -a 256 "$wheel" | cut -d' ' -f1)"
installer_sha256="$(shasum -a 256 "$installer_path" | cut -d' ' -f1)"
idle_stop_sha256="$(shasum -a 256 "$idle_stop_path" | cut -d' ' -f1)"
```

Use these immutable keys and upload function; every value passed into SSM is captured from these commands:

```bash
wheel_key="bootstrap/$AWF_SUPERVISOR_SOURCE_REF/$(basename "$wheel")"
installer_key="bootstrap/$AWF_SUPERVISOR_SOURCE_REF/install-supervisor-agent.sh"
idle_stop_key="bootstrap/$AWF_SUPERVISOR_SOURCE_REF/aws-agent-idle-stop"
put_bootstrap_object() {
  local path="$1" key="$2" sha256="$3" version_id
  version_id="$(aws --profile "$AGENT_AWS_PROFILE" --region "$AGENT_AWS_REGION" \
    s3api put-object \
    --bucket "$bucket" \
    --key "$key" \
    --body "$path" \
    --server-side-encryption aws:kms \
    --metadata "sha256=$sha256" \
    --query VersionId \
    --output text)"
  [[ -n "$version_id" && "$version_id" != "None" && "$version_id" != "null" ]] || {
    echo "S3 versioning did not return VersionId for $key" >&2
    exit 1
  }
  printf '%s\n' "$version_id"
}
wheel_version_id="$(put_bootstrap_object "$wheel" "$wheel_key" "$wheel_sha256")"
installer_version_id="$(put_bootstrap_object "$installer_path" "$installer_key" "$installer_sha256")"
idle_stop_version_id="$(put_bootstrap_object "$idle_stop_path" "$idle_stop_key" "$idle_stop_sha256")"
```

Build one JSON manifest with `jq -cn`; it contains only the bucket, three fixed bootstrap keys, three VersionIds, three SHA-256 values, `api_url`, `queue_url`, region, and literal agent ID `aws-agent-01`. Encode that data and construct the remote program without interpolating it as shell:

```bash
manifest_json="$(jq -cn \
  --arg bucket "$bucket" \
  --arg region "$AGENT_AWS_REGION" \
  --arg api_url "$api_url" \
  --arg queue_url "$queue_url" \
  --arg agent_id "aws-agent-01" \
  --arg wheel_key "$wheel_key" \
  --arg wheel_version_id "$wheel_version_id" \
  --arg wheel_sha256 "$wheel_sha256" \
  --arg installer_key "$installer_key" \
  --arg installer_version_id "$installer_version_id" \
  --arg installer_sha256 "$installer_sha256" \
  --arg idle_stop_key "$idle_stop_key" \
  --arg idle_stop_version_id "$idle_stop_version_id" \
  --arg idle_stop_sha256 "$idle_stop_sha256" \
  '{bucket:$bucket,region:$region,api_url:$api_url,queue_url:$queue_url,agent_id:$agent_id,wheel:{key:$wheel_key,version_id:$wheel_version_id,sha256:$wheel_sha256},installer:{key:$installer_key,version_id:$installer_version_id,sha256:$installer_sha256},idle_stop:{key:$idle_stop_key,version_id:$idle_stop_version_id,sha256:$idle_stop_sha256}}')"
manifest_b64="$(printf '%s' "$manifest_json" | base64 | tr -d '\n')"
printf -v remote_command 'manifest_b64=%q\n' "$manifest_b64"
remote_command+="$(cat <<'REMOTE'
set -Eeuo pipefail
work_dir="$(mktemp -d /var/tmp/awf-supervisor.XXXXXX)"
trap 'rm -rf "$work_dir"' EXIT
manifest_path="$work_dir/manifest.json"
printf '%s' "$manifest_b64" | base64 --decode >"$manifest_path"
bucket="$(jq -er '.bucket' "$manifest_path")"
wheel_key="$(jq -er '.wheel.key' "$manifest_path")"
wheel_version_id="$(jq -er '.wheel.version_id' "$manifest_path")"
wheel_sha256="$(jq -er '.wheel.sha256' "$manifest_path")"
installer_key="$(jq -er '.installer.key' "$manifest_path")"
installer_version_id="$(jq -er '.installer.version_id' "$manifest_path")"
installer_sha256="$(jq -er '.installer.sha256' "$manifest_path")"
idle_stop_key="$(jq -er '.idle_stop.key' "$manifest_path")"
idle_stop_version_id="$(jq -er '.idle_stop.version_id' "$manifest_path")"
idle_stop_sha256="$(jq -er '.idle_stop.sha256' "$manifest_path")"
api_url="$(jq -er '.api_url' "$manifest_path")"
queue_url="$(jq -er '.queue_url' "$manifest_path")"
agent_id="$(jq -er '.agent_id' "$manifest_path")"
AGENT_AWS_REGION="$(jq -er '.region' "$manifest_path")"
/usr/bin/python3 -m venv --help >/dev/null
test -x /usr/bin/aws
download() {
  local key="$1" version_id="$2" destination="$3" expected_sha256="$4" actual_sha256
  /usr/bin/aws s3api get-object \
    --region "$AGENT_AWS_REGION" \
    --bucket "$bucket" \
    --key "$key" \
    --version-id "$version_id" \
    "$destination" >/dev/null
  actual_sha256="$(sha256sum "$destination" | cut -d' ' -f1)"
  [[ "$actual_sha256" == "$expected_sha256" ]] || {
    echo "Downloaded artifact checksum mismatch: $key" >&2
    exit 1
  }
}
download "$wheel_key" "$wheel_version_id" "$work_dir/awf.whl" "$wheel_sha256"
download "$installer_key" "$installer_version_id" "$work_dir/install-supervisor-agent.sh" "$installer_sha256"
download "$idle_stop_key" "$idle_stop_version_id" "$work_dir/aws-agent-idle-stop" "$idle_stop_sha256"
AWF_WHEEL_PATH="$work_dir/awf.whl" \
AWF_WHEEL_SHA256="$wheel_sha256" \
AWF_IDLE_STOP_PATH="$work_dir/aws-agent-idle-stop" \
AWF_IDLE_STOP_SHA256="$idle_stop_sha256" \
AWF_SUPERVISOR_API_URL="$api_url" \
AWF_SUPERVISOR_AWS_QUEUE_URL="$queue_url" \
AWF_SUPERVISOR_REGION="$AGENT_AWS_REGION" \
AWF_SUPERVISOR_AGENT_ID="$agent_id" \
/bin/bash "$work_dir/install-supervisor-agent.sh"
REMOTE
)"
send_ssm_and_require_success() {
  local command="$1" parameters_path command_id invocation
  parameters_path="$build_dir/ssm-parameters.json"
  jq -cn --arg command "$command" '{"commands":[$command]}' >"$parameters_path"
  command_id="$(aws --profile "$AGENT_AWS_PROFILE" --region "$AGENT_AWS_REGION" \
    ssm send-command \
    --document-name AWS-RunShellScript \
    --instance-ids "$instance_id" \
    --timeout-seconds 900 \
    --max-concurrency 1 \
    --max-errors 0 \
    --parameters "file://$parameters_path" \
    --query 'Command.CommandId' \
    --output text)"
  [[ -n "$command_id" && "$command_id" != "None" ]] || exit 1
  aws --profile "$AGENT_AWS_PROFILE" --region "$AGENT_AWS_REGION" \
    ssm wait command-executed --command-id "$command_id" --instance-id "$instance_id" || true
  invocation="$(aws --profile "$AGENT_AWS_PROFILE" --region "$AGENT_AWS_REGION" \
    ssm get-command-invocation --command-id "$command_id" --instance-id "$instance_id" --output json)"
  jq -e '.Status == "Success"' <<<"$invocation" >/dev/null
}
send_ssm_and_require_success "$remote_command"
```

After the immutable installer command succeeds, submit the post-restart health program through the same `send_ssm_and_require_success` function; this is the second and final bounded SSM command:

```bash
post_doctor_command="$(cat <<'REMOTE'
set -Eeuo pipefail
systemctl is-active --quiet awf-supervisor-agent
set -a
. /etc/awf-supervisor-agent.env
set +a
sudo -u ubuntu env \
  AWF_SUPERVISOR_API_URL="$AWF_SUPERVISOR_API_URL" \
  AWF_SUPERVISOR_AWS_QUEUE_URL="$AWF_SUPERVISOR_AWS_QUEUE_URL" \
  AWF_SUPERVISOR_REGION="$AWF_SUPERVISOR_REGION" \
  AWF_SUPERVISOR_AGENT_ID="$AWF_SUPERVISOR_AGENT_ID" \
  AWF_SUPERVISOR_STATE_DIR="$AWF_SUPERVISOR_STATE_DIR" \
  AWF_SUPERVISOR_ACTIVE_LEASE_PATH="$AWF_SUPERVISOR_ACTIVE_LEASE_PATH" \
  AWF_SUPERVISOR_REPO_ROOT="$AWF_SUPERVISOR_REPO_ROOT" \
  AWS_AGENT_WORKSPACE="$AWS_AGENT_WORKSPACE" \
  /opt/awf/current/bin/awf supervisor agent doctor \
    --agent-id "$AWF_SUPERVISOR_AGENT_ID" \
    --environment aws \
    --state-dir "$AWF_SUPERVISOR_STATE_DIR" \
    --active-lease-path "$AWF_SUPERVISOR_ACTIVE_LEASE_PATH" \
    --repo-root "$AWF_SUPERVISOR_REPO_ROOT" \
    --json
REMOTE
)"
send_ssm_and_require_success "$post_doctor_command"
```

The installer must render only shell-safe validated assignments before this controlled root sourcing step. `send_ssm_and_require_success` JSON-parses every invocation and rejects any non-`Success` status.

- [ ] **Step 4: Run GREEN and commit**

```bash
bash tests/update-supervisor-agent_test.sh
git add scripts/update-supervisor-agent.sh tests/update-supervisor-agent_test.sh .env.example
git commit -m "feat: deploy pinned supervisor agent"
```

### Task 5: Add redacted service status and boot recovery checks

**Files:**
- Create: `scripts/status-supervisor-agent.sh`
- Create: `tests/status-supervisor-agent_test.sh`
- Modify: `README.md`

- [ ] **Step 1: Write failing status tests**

Fake `aws`, `awscurl`, `jq`, and the SSM JSON result. The AWS API fixture for the control-plane request must be:

```json
{
  "schema_version": 1,
  "agents": [{
    "agent_id": "aws-agent-01",
    "environment": "aws",
    "status": "ONLINE",
    "last_heartbeat_at": "2026-07-30T12:03:20Z",
    "max_concurrency": 1,
    "active_jobs": 0,
    "capabilities": ["git", "omp", "github"],
    "repos": [],
    "version": {"awf": "1.2.3", "omp": "1.0.0"}
  }]
}
```

Fixture statuses:

```text
EC2 stopped -> state=stopped, no SSM or awscurl call
EC2 running + service active + ONLINE heartbeat age <= 45 seconds -> healthy
service failed -> unhealthy with systemd unit state only
ONLINE heartbeat age > 45 seconds -> unhealthy
invalid control-plane response, missing agent, non-ONLINE status, or failed awscurl -> unhealthy
active lease -> report job_id/generation/state, no prompt
outbox pending -> report count, no event body
```

Assert the `awscurl` invocation is exactly `awscurl --service execute-api --region "$AGENT_AWS_REGION" --profile "$AGENT_AWS_PROFILE" "$AWF_SUPERVISOR_API_URL/v1/admin/agents"` and that its JSON, never its raw output, is parsed. Assert the output uses only the remote metadata allowlist plus `control_plane.status`, `control_plane.last_heartbeat_at`, and computed `control_plane.heartbeat_age_seconds`.

- [ ] **Step 2: Confirm RED**

```bash
bash tests/status-supervisor-agent_test.sh
```

Expected: status script missing.

- [ ] **Step 3: Implement bounded status collection**

Require `AGENT_AWS_PROFILE`, `AGENT_AWS_REGION`, `aws`, `awscurl`, and `jq`. Resolve `InstanceId` and `SupervisorApiUrl` from the stack outputs using the existing SSO profile. If EC2 is stopped, print `{"state":"stopped"}` and make no SSM or control-plane request.

For a running instance, use SSM to collect and JSON-validate only:

```json
{
  "service": "active",
  "agent_id": "aws-agent-01",
  "active_lease": {"job_id": "job-1", "generation": 4, "state": "RUNNING"},
  "pending_outbox": 0,
  "installed_source_ref": "40-hex"
}
```

Then call the IAM-authenticated admin endpoint exactly once:

```bash
AWF_SUPERVISOR_API_URL="${AWF_SUPERVISOR_API_URL%/}"
agents_json="$(awscurl --service execute-api \
  --region "$AGENT_AWS_REGION" \
  --profile "$AGENT_AWS_PROFILE" \
  "$AWF_SUPERVISOR_API_URL/v1/admin/agents")"
agent_json="$(jq -ce '
  if .schema_version != 1 or (.agents | type) != "array" then
    error("invalid control-plane response")
  else
    [.agents[] | select(.agent_id == "aws-agent-01" and .environment == "aws")]
    | if length == 1 then .[0] else error("AWS agent is missing or ambiguous") end
  end
' <<<"$agents_json")"
status="$(jq -er '.status' <<<"$agent_json")"
heartbeat_epoch="$(jq -er '.last_heartbeat_at | fromdateiso8601' <<<"$agent_json")"
heartbeat_age_seconds="$(( $(date -u +%s) - heartbeat_epoch ))"
```

Report `healthy` only when the SSM service value is exactly `active`, `$status` is exactly `ONLINE`, and `heartbeat_age_seconds` is in `0..45`. Map all other cases to exactly one fixed reason code: `service_inactive`, `control_plane_unreachable`, `control_plane_invalid`, `agent_not_online`, or `heartbeat_stale`. Emit only the remote metadata allowlist plus `control_plane.status`, `control_plane.last_heartbeat_at`, and computed `control_plane.heartbeat_age_seconds`; never emit a raw response, environment content, model output, session history, repo path, token, or journal line.

- [ ] **Step 4: Document operations**

Add exact commands:

```bash
uv tool install awscurl
./scripts/update-supervisor-agent.sh
./scripts/status-supervisor-agent.sh
sudo systemctl status awf-supervisor-agent
sudo journalctl -u awf-supervisor-agent --since today
```

Explain that the operator runs `uv tool install awscurl` once under the same account that owns `AGENT_AWS_PROFILE`, and that journal output is available only on the authorized host and should not be pasted into public issues without review.

- [ ] **Step 5: Run GREEN and commit**

```bash
bash tests/status-supervisor-agent_test.sh
git add scripts/status-supervisor-agent.sh tests/status-supervisor-agent_test.sh README.md
git commit -m "feat: report supervisor EC2 health"
```

### Task 6: Verify host integration before live rollout

**Files:**
- Modify only when a focused failing test identifies a defect in Tasks 1-5.

- [ ] **Step 1: Run all local shell and policy tests**

```bash
bash tests/agentctl_test.sh
bash tests/aws-agent-idle-stop_test.sh
bash tests/install-supervisor-agent_test.sh
bash tests/update-supervisor-agent_test.sh
bash tests/status-supervisor-agent_test.sh
npm --prefix supervisor test -- --test-name-pattern='template'
sam validate --lint --template infra/template.yaml
```

Expected: all commands pass.

- [ ] **Step 2: Deploy the Control Plane without enabling the agent**

```bash
./scripts/deploy.sh
./scripts/status.sh
```

Expected: stack update succeeds; existing Cloudflare launcher and console readiness still pass; AWS Supervisor agent remains offline until the wheel rollout.

- [ ] **Step 3: Roll out the pinned agent**

```bash
./scripts/update-supervisor-agent.sh
./scripts/status-supervisor-agent.sh
```

Expected: service is active, Control Plane reports `aws-agent-01` online, active lease is empty, outbox is empty.

- [ ] **Step 4: Exercise automatic-stop gating without a real job**

Use an SSM command to create a fixture active-lease marker, run `aws-agent-idle-stop`, and assert the instance remains running. Remove only the fixture marker, run idle-status, and confirm it returns safe. Do not wait for or force the real idle timeout in this step.

- [ ] **Step 5: Record deployment evidence**

Store only stack ID, source commit, wheel SHA-256, service state, and timestamps under the existing private deployment artifact location. Do not add live account IDs or secrets to Git.
