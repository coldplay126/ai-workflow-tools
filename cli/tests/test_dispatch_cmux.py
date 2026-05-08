"""Integration tests for CmuxDispatch using a synthetic .agent/ fixture.

These tests stand up the cmux-agent on-disk state (control-plane.sqlite3 +
inbox/outbox dirs) without booting cmux-agent itself. A "fake worker"
thread polls outbox, mimics what the broker + an AI CLI would do, and
writes a result artifact back into ``inbox/awf-orchestrator/``. Polling
shapes the timing-sensitive tests but is bounded enough to be fast.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core import _cmux_bridge as bridge
from awf.core.dispatch import (
    ChainedStep,
    CmuxDispatch,
    CmuxDispatchError,
    CmuxDispatchOptions,
    WorkerSpec,
    cmux_dispatch_available,
)


# --------------------------------------------------------------------------
# Fixtures + helpers
# --------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'CREATED',
    workspace_id TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    agent_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    role        TEXT NOT NULL,
    name        TEXT NOT NULL,
    surface_id  TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    message_id    TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    sender        TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    type          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    payload       TEXT NOT NULL,
    artifact_path TEXT,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_run(cwd: Path, *, workers: list[tuple[str, str | None]]) -> str:
    """Stand up a minimal .agent/ tree with one active run + given workers.

    ``workers`` is a list of ``(name, surface_id)`` tuples; surface_id may
    be ``None`` to simulate a worker with no terminal attached.
    Returns the run_id.
    """
    base = cwd / ".agent"
    (base / "outbox").mkdir(parents=True, exist_ok=True)
    (base / "inbox").mkdir(parents=True, exist_ok=True)
    (base / "processed").mkdir(parents=True, exist_ok=True)

    db = base / "control-plane.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    run_id = str(uuid.uuid4())
    now = _now_iso()
    conn.execute(
        "INSERT INTO runs (run_id, status, workspace_id, created_at, updated_at)"
        " VALUES (?, 'RUNNING', NULL, ?, ?)",
        (run_id, now, now),
    )
    for name, surface_id in workers:
        conn.execute(
            "INSERT INTO agents (agent_id, run_id, role, name, surface_id, created_at)"
            " VALUES (?, ?, 'WORKER', ?, ?, ?)",
            (str(uuid.uuid4()), run_id, name, surface_id, now),
        )
        (base / "inbox" / name).mkdir(parents=True, exist_ok=True)
    conn.commit()
    conn.close()
    return run_id


class _FakeBroker:
    """Background thread that mimics the cmux-agent broker for a fixture run.

    Watches ``outbox/`` for dispatch artifacts, calls a user-supplied
    ``responder`` to produce a response message, then writes the response
    artifact directly into ``inbox/awf-orchestrator/`` (skipping the
    inject-to-terminal step the real broker handles).
    """

    def __init__(
        self,
        cwd: Path,
        *,
        responder,
        delay: float = 0.0,
        recipient_filter: list[str] | None = None,
    ) -> None:
        self._base = cwd / ".agent"
        self._responder = responder
        self._delay = delay
        self._recipient_filter = (
            set(recipient_filter) if recipient_filter is not None else None
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._seen: set[str] = set()

    def __enter__(self) -> "_FakeBroker":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _loop(self) -> None:
        outbox = self._base / "outbox"
        inbox = self._base / "inbox" / "awf-orchestrator"
        processed = self._base / "processed"
        while not self._stop.is_set():
            for path in sorted(outbox.glob("awf-*.json")):
                if path.name in self._seen:
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if data.get("type") != "dispatch":
                    continue
                recipient = data.get("recipient", "")
                if (
                    self._recipient_filter is not None
                    and recipient not in self._recipient_filter
                ):
                    continue
                self._seen.add(path.name)

                if self._delay:
                    time.sleep(self._delay)

                response_message = self._responder(data)
                ctx = data.get("context") or {}
                response = {
                    "type": "result",
                    "sender": recipient,
                    "recipient": "awf-orchestrator",
                    "message": response_message,
                    "context": {
                        "awf_dispatch_id": ctx.get("awf_dispatch_id"),
                        "awf_worker_idx": ctx.get("awf_worker_idx"),
                    },
                }
                inbox.mkdir(parents=True, exist_ok=True)
                target = inbox / f"resp-{path.name}"
                target.write_text(
                    json.dumps(response, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                # Mimic broker moving outbox → processed/.
                processed.mkdir(parents=True, exist_ok=True)
                try:
                    path.replace(processed / path.name)
                except OSError:
                    pass
            time.sleep(0.05)


# --------------------------------------------------------------------------
# Active-run discovery + availability
# --------------------------------------------------------------------------


def test_find_active_run_returns_none_when_no_agent_dir(tmp_path):
    assert bridge.find_active_run(tmp_path) is None


def test_find_active_run_returns_state_when_running(tmp_path):
    run_id = _make_run(tmp_path, workers=[])
    state = bridge.find_active_run(tmp_path)
    assert state is not None
    assert state.run_id == run_id


def test_cmux_dispatch_available_requires_path_and_run(tmp_path):
    # No active run → False even if the binary is on PATH.
    with patch("awf.core.dispatch.shutil.which", return_value="/usr/local/bin/cmux-agent"):
        assert cmux_dispatch_available(str(tmp_path)) is False

    _make_run(tmp_path, workers=[("worker-x", None)])
    # Active run + binary on PATH → True.
    with patch("awf.core.dispatch.shutil.which", return_value="/usr/local/bin/cmux-agent"):
        assert cmux_dispatch_available(str(tmp_path)) is True
    # Active run but binary missing → False.
    with patch("awf.core.dispatch.shutil.which", return_value=None):
        assert cmux_dispatch_available(str(tmp_path)) is False


# --------------------------------------------------------------------------
# Round-trip dispatch
# --------------------------------------------------------------------------


def _spec(role: str, prompt: str = "do X", *, timeout_sec: int = 5,
          require_json: bool = False) -> WorkerSpec:
    return WorkerSpec(
        role=role,
        provider=object(),  # cmux dispatch never invokes provider directly
        prompt=prompt,
        timeout_sec=timeout_sec,
        require_json=require_json,
    )


def test_cmux_dispatch_round_trips_two_workers(tmp_path):
    _make_run(
        tmp_path,
        workers=[("worker-plan_conformance", None), ("worker-quality_validation", None)],
    )

    def responder(dispatch: dict) -> str:
        role = (dispatch.get("context") or {}).get("awf_role", "")
        return f'{{"role":"{role}","ok":true}}'

    with _FakeBroker(tmp_path, responder=responder):
        results = CmuxDispatch().run(
            [
                _spec("plan_conformance", require_json=True),
                _spec("quality_validation", require_json=True),
            ],
            cwd=str(tmp_path),
        )

    assert [r.role for r in results] == ["plan_conformance", "quality_validation"]
    assert all(r.returncode == 0 and not r.timed_out for r in results)
    assert all(r.parsed and r.parsed.get("ok") is True for r in results)
    assert all(r.provider_name.startswith("cmux:worker-") for r in results)


def test_cmux_dispatch_preserves_input_order_when_responses_arrive_out_of_order(tmp_path):
    _make_run(
        tmp_path,
        workers=[("worker-fast", None), ("worker-slow", None)],
    )

    delays = {"worker-slow": 0.4, "worker-fast": 0.05}

    def responder(dispatch: dict) -> str:
        recipient = dispatch.get("recipient", "")
        time.sleep(delays.get(recipient, 0))
        return f'reply-from-{recipient}'

    with _FakeBroker(tmp_path, responder=responder):
        results = CmuxDispatch().run(
            [_spec("slow", timeout_sec=5), _spec("fast", timeout_sec=5)],
            cwd=str(tmp_path),
        )

    assert [r.role for r in results] == ["slow", "fast"]


def test_cmux_dispatch_partial_timeout(tmp_path):
    _make_run(
        tmp_path,
        workers=[("worker-resp", None), ("worker-silent", None)],
    )

    def responder(dispatch: dict) -> str:
        if dispatch["recipient"] == "worker-silent":
            # Sleep long enough that the test's deadline triggers first.
            time.sleep(10.0)
        return f'reply-from-{dispatch["recipient"]}'

    # timeout_sec=1 + warmup_grace=30 → effective deadline ~31s. We need
    # a faster fail path for the silent worker; patch the warmup grace.
    with patch("awf.core.dispatch._CMUX_WARMUP_GRACE_SEC", 0.0), \
         _FakeBroker(tmp_path, responder=responder, recipient_filter=["worker-resp"]):
        results = CmuxDispatch().run(
            [_spec("resp", timeout_sec=2), _spec("silent", timeout_sec=1)],
            cwd=str(tmp_path),
        )

    by_role = {r.role: r for r in results}
    assert by_role["resp"].returncode == 0
    assert not by_role["resp"].timed_out
    assert by_role["silent"].timed_out
    assert by_role["silent"].returncode == 124


def test_cmux_dispatch_require_json_records_parse_error(tmp_path):
    _make_run(tmp_path, workers=[("worker-broken", None)])

    def responder(dispatch: dict) -> str:
        return "not json"

    with _FakeBroker(tmp_path, responder=responder):
        results = CmuxDispatch().run(
            [_spec("broken", require_json=True)], cwd=str(tmp_path),
        )

    [result] = results
    assert result.returncode == 0
    assert result.parse_error is True
    assert result.parsed is None
    assert result.stdout == "not json"


def test_cmux_dispatch_orchestrator_self_registers_idempotently(tmp_path):
    _make_run(tmp_path, workers=[("worker-x", None)])

    def responder(_dispatch):
        return "ok"

    with _FakeBroker(tmp_path, responder=responder):
        CmuxDispatch().run([_spec("x")], cwd=str(tmp_path))
        # Second invocation must not double-insert the orchestrator row.
        CmuxDispatch().run([_spec("x")], cwd=str(tmp_path))

    db = tmp_path / ".agent" / "control-plane.sqlite3"
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT name FROM agents WHERE name = 'awf-orchestrator'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1


# --------------------------------------------------------------------------
# Auto-spawn
# --------------------------------------------------------------------------


def test_cmux_dispatch_auto_spawns_when_role_missing(tmp_path):
    _make_run(tmp_path, workers=[])  # no workers

    spawn_calls: list[dict] = []

    def fake_spawn(*, cwd, name, role, provider, template, flags, timeout_sec):
        spawn_calls.append(
            {
                "cwd": cwd,
                "name": name,
                "role": role,
                "provider": provider,
                "template": template,
                "flags": flags,
            }
        )
        # Simulate cmux-agent inserting the worker row.
        db = Path(cwd) / ".agent" / "control-plane.sqlite3"
        conn = sqlite3.connect(str(db))
        run_id = conn.execute(
            "SELECT run_id FROM runs WHERE status = 'RUNNING'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO agents (agent_id, run_id, role, name, surface_id, created_at)"
            " VALUES (?, ?, 'WORKER', ?, ?, ?)",
            (str(uuid.uuid4()), run_id, name, "surface:fake-1", _now_iso()),
        )
        conn.commit()
        conn.close()
        return True, "spawned"

    options = CmuxDispatchOptions(
        role_to_worker={"plan": {"provider": "codex", "template": "review"}}
    )

    with patch("awf.core._cmux_bridge.spawn_worker_subprocess", side_effect=fake_spawn):
        def responder(_dispatch):
            return "ok"

        with _FakeBroker(tmp_path, responder=responder):
            results = CmuxDispatch(options).run(
                [_spec("plan")], cwd=str(tmp_path),
            )

    assert len(spawn_calls) == 1
    assert spawn_calls[0]["name"] == "worker-plan"
    assert spawn_calls[0]["provider"] == "codex"
    assert spawn_calls[0]["template"] == "review"
    assert results[0].returncode == 0


def test_cmux_dispatch_spawn_failure_surfaces_actionable_error(tmp_path):
    _make_run(tmp_path, workers=[])

    def failing_spawn(*, cwd, name, role, provider, template, flags, timeout_sec):
        return False, "cmux not running"

    with patch("awf.core._cmux_bridge.spawn_worker_subprocess", side_effect=failing_spawn):
        with pytest.raises(CmuxDispatchError) as excinfo:
            CmuxDispatch().run([_spec("missing")], cwd=str(tmp_path))

    msg = str(excinfo.value)
    assert "missing" in msg
    assert "cmux not running" in msg


# --------------------------------------------------------------------------
# Lifecycle: ephemeral teardown vs reusable retention
# --------------------------------------------------------------------------


def test_cmux_dispatch_ephemeral_tears_down_spawned_workers(tmp_path):
    _make_run(tmp_path, workers=[])

    def fake_spawn(*, cwd, name, role, provider, template, flags, timeout_sec):
        db = Path(cwd) / ".agent" / "control-plane.sqlite3"
        conn = sqlite3.connect(str(db))
        run_id = conn.execute(
            "SELECT run_id FROM runs WHERE status = 'RUNNING'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO agents (agent_id, run_id, role, name, surface_id, created_at)"
            " VALUES (?, ?, 'WORKER', ?, ?, ?)",
            (str(uuid.uuid4()), run_id, name, "surface:fake-1", _now_iso()),
        )
        conn.commit()
        conn.close()
        (Path(cwd) / ".agent" / "inbox" / name).mkdir(parents=True, exist_ok=True)
        return True, "spawned"

    teardown_targets: list[str] = []

    def fake_teardown(state, worker, *, cmux_close_timeout=10.0):
        teardown_targets.append(worker.name)

    options = CmuxDispatchOptions(lifecycle="ephemeral")

    with patch("awf.core._cmux_bridge.spawn_worker_subprocess", side_effect=fake_spawn), \
         patch("awf.core.dispatch.bridge.teardown_worker", side_effect=fake_teardown):
        with _FakeBroker(tmp_path, responder=lambda d: "ok"):
            CmuxDispatch(options).run([_spec("plan")], cwd=str(tmp_path))

    assert teardown_targets == ["worker-plan"]


def test_cmux_dispatch_reusable_keeps_workers_for_next_batch(tmp_path):
    _make_run(tmp_path, workers=[])

    spawn_count = {"n": 0}

    def fake_spawn(*, cwd, name, role, provider, template, flags, timeout_sec):
        spawn_count["n"] += 1
        db = Path(cwd) / ".agent" / "control-plane.sqlite3"
        conn = sqlite3.connect(str(db))
        run_id = conn.execute(
            "SELECT run_id FROM runs WHERE status = 'RUNNING'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO agents (agent_id, run_id, role, name, surface_id, created_at)"
            " VALUES (?, ?, 'WORKER', ?, ?, ?)",
            (str(uuid.uuid4()), run_id, name, None, _now_iso()),
        )
        conn.commit()
        conn.close()
        (Path(cwd) / ".agent" / "inbox" / name).mkdir(parents=True, exist_ok=True)
        return True, "spawned"

    options = CmuxDispatchOptions(lifecycle="reusable")

    with patch("awf.core._cmux_bridge.spawn_worker_subprocess", side_effect=fake_spawn):
        with _FakeBroker(tmp_path, responder=lambda d: "ok"):
            CmuxDispatch(options).run([_spec("plan")], cwd=str(tmp_path))
            CmuxDispatch(options).run([_spec("plan")], cwd=str(tmp_path))

    # Reusable keeps the worker across batches: only one spawn.
    assert spawn_count["n"] == 1


def test_cmux_dispatch_cleanup_runs_even_when_dispatch_raises(tmp_path):
    _make_run(tmp_path, workers=[])

    def fake_spawn(*, cwd, name, role, provider, template, flags, timeout_sec):
        db = Path(cwd) / ".agent" / "control-plane.sqlite3"
        conn = sqlite3.connect(str(db))
        run_id = conn.execute(
            "SELECT run_id FROM runs WHERE status = 'RUNNING'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO agents (agent_id, run_id, role, name, surface_id, created_at)"
            " VALUES (?, ?, 'WORKER', ?, ?, ?)",
            (str(uuid.uuid4()), run_id, name, "surface:abc", _now_iso()),
        )
        conn.commit()
        conn.close()
        return True, "spawned"

    teardown_calls: list[str] = []

    def fake_teardown(state, worker, *, cmux_close_timeout=10.0):
        teardown_calls.append(worker.name)

    options = CmuxDispatchOptions(lifecycle="ephemeral")

    with patch("awf.core._cmux_bridge.spawn_worker_subprocess", side_effect=fake_spawn), \
         patch("awf.core.dispatch.bridge.teardown_worker", side_effect=fake_teardown), \
         patch(
             "awf.core.dispatch.bridge.write_dispatch_artifact",
             side_effect=RuntimeError("disk full"),
         ):
        with pytest.raises(RuntimeError, match="disk full"):
            CmuxDispatch(options).run([_spec("plan")], cwd=str(tmp_path))

    assert teardown_calls == ["worker-plan"]


# --------------------------------------------------------------------------
# Result shape compatibility with InlineDispatch
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Chained dispatch (Phase 3)
# --------------------------------------------------------------------------


def test_cmux_chained_threads_prior_results_into_next_prompts(tmp_path):
    _make_run(
        tmp_path,
        workers=[("worker-precision", None), ("worker-quality_validation", None)],
    )

    captured_prompts: list[str] = []

    def responder(dispatch: dict) -> str:
        captured_prompts.append(dispatch.get("message", ""))
        role = (dispatch.get("context") or {}).get("awf_role", "")
        return f'{{"role":"{role}","step_done":true}}'

    def step1(prior):
        return WorkerSpec(
            role="precision",
            provider=object(),
            prompt="STEP1_PROMPT",
            timeout_sec=5,
            require_json=True,
        )

    def step2(prior):
        # Prompt builds on step 1's stdout — exercises factory(prior_results).
        prior_stdout = prior[-1].stdout if prior else "(none)"
        return WorkerSpec(
            role="quality_validation",
            provider=object(),
            prompt=f"STEP2 incorporates: {prior_stdout}",
            timeout_sec=5,
            require_json=True,
        )

    with _FakeBroker(tmp_path, responder=responder):
        results = CmuxDispatch().run_chained(
            [
                ChainedStep(role="precision", factory=step1),
                ChainedStep(role="quality_validation", factory=step2),
            ],
            cwd=str(tmp_path),
        )

    assert [r.role for r in results] == ["precision", "quality_validation"]
    assert all(r.parsed and r.parsed["step_done"] is True for r in results)
    assert captured_prompts[0] == "STEP1_PROMPT"
    # Step 2's prompt must have included step 1's actual stdout.
    assert "step_done" in captured_prompts[1]


def test_cmux_chained_pins_one_worker_per_role_across_steps(tmp_path):
    _make_run(tmp_path, workers=[("worker-primary", "surface:1")])

    recipients: list[str] = []

    def responder(dispatch: dict) -> str:
        recipients.append(dispatch["recipient"])
        return "ok"

    def factory(role: str):
        def _f(prior):
            return WorkerSpec(role=role, provider=object(), prompt="p", timeout_sec=5)
        return _f

    with _FakeBroker(tmp_path, responder=responder):
        results = CmuxDispatch().run_chained(
            [
                ChainedStep(role="primary", factory=factory("primary")),
                ChainedStep(role="primary", factory=factory("primary")),
                ChainedStep(role="primary", factory=factory("primary")),
            ],
            cwd=str(tmp_path),
        )

    assert len(results) == 3
    # All three steps must have routed to the same pinned worker — that's the
    # whole point of chained mode (terminal context is reused across the chain).
    assert recipients == ["worker-primary"] * 3


def test_cmux_chained_skips_step_when_factory_returns_none(tmp_path):
    _make_run(
        tmp_path,
        workers=[("worker-a", None), ("worker-c", None)],
    )

    def responder(dispatch):
        return "ok"

    def step_a(prior):
        return WorkerSpec(role="a", provider=object(), prompt="p1", timeout_sec=5)

    def step_b_skipped(prior):
        return None

    def step_c(prior):
        # Skipped step must not appear in prior.
        assert [r.role for r in prior] == ["a"]
        return WorkerSpec(role="c", provider=object(), prompt="p3", timeout_sec=5)

    with _FakeBroker(tmp_path, responder=responder):
        results = CmuxDispatch().run_chained(
            [
                ChainedStep(role="a", factory=step_a),
                ChainedStep(role="b", factory=step_b_skipped),
                ChainedStep(role="c", factory=step_c),
            ],
            cwd=str(tmp_path),
        )

    assert [r.role for r in results] == ["a", "c"]


def test_cmux_chained_factory_role_mismatch_raises(tmp_path):
    _make_run(tmp_path, workers=[("worker-x", None)])

    def bad_factory(prior):
        # Declared role "x" but spec says "y" — caller bug we want to surface.
        return WorkerSpec(role="y", provider=object(), prompt="p", timeout_sec=5)

    with pytest.raises(CmuxDispatchError, match="role"):
        CmuxDispatch().run_chained(
            [ChainedStep(role="x", factory=bad_factory)], cwd=str(tmp_path),
        )


def test_cmux_chained_no_active_run_raises_actionable_error(tmp_path):
    def step(prior):
        return WorkerSpec(role="x", provider=object(), prompt="p", timeout_sec=5)

    with pytest.raises(CmuxDispatchError, match="cmux-agent start"):
        CmuxDispatch().run_chained(
            [ChainedStep(role="x", factory=step)], cwd=str(tmp_path),
        )


def test_cmux_chained_cleanup_runs_when_step_raises(tmp_path):
    _make_run(tmp_path, workers=[])

    def fake_spawn(*, cwd, name, role, provider, template, flags, timeout_sec):
        db = Path(cwd) / ".agent" / "control-plane.sqlite3"
        conn = sqlite3.connect(str(db))
        run_id = conn.execute(
            "SELECT run_id FROM runs WHERE status = 'RUNNING'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO agents (agent_id, run_id, role, name, surface_id, created_at)"
            " VALUES (?, ?, 'WORKER', ?, ?, ?)",
            (str(uuid.uuid4()), run_id, name, "surface:abc", _now_iso()),
        )
        conn.commit()
        conn.close()
        return True, "spawned"

    teardown_calls: list[str] = []

    def fake_teardown(state, worker, *, cmux_close_timeout=10.0):
        teardown_calls.append(worker.name)

    def step1(prior):
        return WorkerSpec(role="precision", provider=object(), prompt="p1", timeout_sec=5)

    def step2_raises(prior):
        raise RuntimeError("factory exploded")

    options = CmuxDispatchOptions(lifecycle="ephemeral")

    with patch("awf.core._cmux_bridge.spawn_worker_subprocess", side_effect=fake_spawn), \
         patch("awf.core.dispatch.bridge.teardown_worker", side_effect=fake_teardown):
        with _FakeBroker(tmp_path, responder=lambda d: "ok"):
            with pytest.raises(RuntimeError, match="factory exploded"):
                CmuxDispatch(options).run_chained(
                    [
                        ChainedStep(role="precision", factory=step1),
                        ChainedStep(role="quality_validation", factory=step2_raises),
                    ],
                    cwd=str(tmp_path),
                )

    assert "worker-precision" in teardown_calls


# --------------------------------------------------------------------------
# Result shape compatibility
# --------------------------------------------------------------------------


def test_cmux_dispatch_result_shape_matches_inline_dispatch(tmp_path):
    """Cross-strategy callers downstream rely on AgentResult fields. Both
    backends must populate the same shape so swapping is a no-op."""
    from awf.core.agent_runner import AgentResult

    _make_run(tmp_path, workers=[("worker-x", None)])

    with _FakeBroker(tmp_path, responder=lambda d: '{"ok": true}'):
        results = CmuxDispatch().run(
            [_spec("x", require_json=True)], cwd=str(tmp_path),
        )

    [result] = results
    expected_fields = {f for f in AgentResult.__annotations__.keys()}
    actual_fields = {f for f in result.__dict__.keys()}
    # Every documented AgentResult field must be set on the cmux-produced result.
    assert expected_fields.issubset(actual_fields)
    assert isinstance(result.elapsed_sec, float)
    assert isinstance(result.parsed, dict)
