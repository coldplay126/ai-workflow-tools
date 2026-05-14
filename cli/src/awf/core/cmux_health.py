"""cmux-agent broker health 정보를 subprocess로 조회.

cmux-agent doctor --json 출력을 mirror하여 awf wf status에 통합.
graceful degrade: cmux-agent 미설치/timeout/JSON 파싱 실패 모두 정상 dict 반환.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_ENV_DISABLE = "AWF_WF_STATUS_NO_CMUX"
_DEFAULT_TIMEOUT_SECONDS = 5.0


def probe_cmux_broker_health(
    repo_root: str | Path,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """`cmux-agent doctor --cwd <repo_root> --json` 호출 결과를 mirror한다.

    환경변수 ``AWF_WF_STATUS_NO_CMUX`` 가 truthy이면 subprocess를 호출하지 않고
    ``status=skipped`` 반환. 호출 실패는 모두 정상 dict로 변환 (exception 미전파).

    반환 dict는 항상 ``status`` 키를 포함:
    - ``alive`` / ``stale`` / ``absent``: cmux-agent doctor의 ``broker_daemon.status`` 미러
    - ``unavailable``: cmux-agent CLI를 PATH에서 찾지 못함
    - ``timeout``: subprocess 호출이 ``timeout_seconds`` 초과
    - ``skipped``: 환경변수로 비활성화
    - ``error``: returncode != 0, JSON 파싱 실패 등 기타 오류

    성공 케이스에는 ``broker_daemon``, ``events_log``, ``sqlite_integrity`` 키도 포함.
    """
    if os.environ.get(_ENV_DISABLE):
        return {"status": "skipped", "detail": f"{_ENV_DISABLE}={os.environ[_ENV_DISABLE]}"}

    cmd = ["cmux-agent", "doctor", "--cwd", str(repo_root), "--json"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return {"status": "unavailable", "detail": "cmux-agent CLI not found"}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": f"{timeout_seconds}s"}

    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        return {"status": "error", "detail": stderr[:100] or f"returncode={proc.returncode}"}

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"status": "error", "detail": f"invalid JSON from cmux-agent: {exc.msg}"}

    health = payload.get("health") if isinstance(payload, dict) else None
    if not isinstance(health, dict):
        return {"status": "error", "detail": "missing 'health' object in cmux-agent doctor output"}

    broker = health.get("broker_daemon") or {}
    return {
        "status": str(broker.get("status", "error")),
        "broker_daemon": broker,
        "events_log": health.get("events_log") or {},
        "sqlite_integrity": health.get("sqlite_integrity") or {},
    }
