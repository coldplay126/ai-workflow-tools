"""CLI 명령어 구현."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cmux_agent.application.broker import MessageBroker
from cmux_agent.application.prompting import PromptBuilder
from cmux_agent.application.runtime import (
    AgentRuntime,
    normalize_agent_entry,
    parse_surface_ref,
    provider_command,
)
from cmux_agent.application.watcher import ArtifactWatcher
from cmux_agent.domain.events import agent_registered, run_created, run_status_changed
from cmux_agent.domain.models import Agent, AgentRole, Run, RunStatus
from cmux_agent.infrastructure.cmux import CmuxAdapter
from cmux_agent.infrastructure.event_log import EventLog
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore

AGENT_DIR = ".agent"
TEMPLATE_STATE_FILE = "template-state.json"
FAILURE_EVENTS = {"artifact.validation_failed", "message.failed"}
SUPPORTED_COMMANDS = (
    "doctor",
    "smoke",
    "start",
    "task",
    "stop",
    "register",
    "spawn",
    "agents",
    "watch",
    "status",
    "events",
    "failures",
    "send",
    "messages",
)


def _normalize_agent_entry(entry: str | dict) -> dict:
    """cmux-agent.json의 agent 항목을 정규화한다.

    string이면 {"provider": entry}로 변환, dict면 그대로 반환.
    """
    return normalize_agent_entry(entry)


def _runtime_dir(cwd: str) -> Path:
    """프로젝트별 런타임 디렉토리. 프로젝트 디렉토리를 오염시키지 않는다."""
    import hashlib
    project_hash = hashlib.md5(str(Path(cwd).resolve()).encode()).hexdigest()[:12]
    project_name = Path(cwd).resolve().name
    return Path.home() / ".local" / "share" / "cmux-agent" / f"{project_name}-{project_hash}"
CONFIG_FILE = "cmux-agent.json"
DEFAULT_CONFIG = {
    "orchestrator": "claude",
    "worker-impl": "claude",
}


def _resolve_template_dir(template: str | None, templates_dir: str | None) -> Path | None:
    """템플릿 이름 또는 경로를 실제 디렉토리로 해석한다."""
    if not template:
        return None
    # 절대/상대 경로로 지정된 경우
    template_path = Path(template)
    if template_path.is_dir() and (template_path / "cmux-agent.json").exists():
        return template_path
    # 이름으로 지정된 경우 (feature, bugfix, review)
    base = templates_dir or os.environ.get("CMUX_TEMPLATES_DIR", "")
    if not base:
        # ai-workflow-tools/templates/cmux/ 자동 탐색
        # monorepo: cmux-agent/cmux_agent/cli/commands.py → ai-workflow-tools/templates/cmux/
        _pkg_root = Path(__file__).resolve().parents[3]
        candidates = [
            _pkg_root / "templates" / "cmux",
            Path.home() / "Documents" / "GitHub" / "ai-workflow-tools" / "templates" / "cmux",
            Path.cwd().parent / "ai-workflow-tools" / "templates" / "cmux",
        ]
        for c in candidates:
            if (c / template).is_dir():
                base = str(c)
                break
    if base:
        resolved = Path(base) / template
        if resolved.is_dir():
            return resolved
    return None


_active_template_dir: Path | None = None
_active_cwd: str = "."


def _template_state_path(fs: AgentFileSystem) -> Path:
    return fs.base / TEMPLATE_STATE_FILE


def _write_template_state(fs: AgentFileSystem, template: str, template_dir: Path) -> None:
    payload = {
        "template": template,
        "template_dir": str(template_dir.resolve()),
    }
    _template_state_path(fs).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _clear_template_state(fs: AgentFileSystem) -> None:
    try:
        _template_state_path(fs).unlink()
    except FileNotFoundError:
        pass


def _restore_template_dir(fs: AgentFileSystem) -> Path | None:
    global _active_template_dir  # noqa: PLW0603
    if _active_template_dir and _active_template_dir.is_dir():
        return _active_template_dir

    path = _template_state_path(fs)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    template_dir = Path(str(data.get("template_dir", ""))).expanduser()
    if not template_dir.is_dir():
        return None
    _active_template_dir = template_dir
    return template_dir


def _load_config(cwd: str = ".") -> dict:
    """cmux-agent.json 설정 파일을 읽는다.

    탐색 순서: 템플릿 디렉토리 → CWD → 기본값.
    """
    candidates = []
    if _active_template_dir:
        candidates.append(_active_template_dir / CONFIG_FILE)
    candidates.append(Path(cwd) / CONFIG_FILE)
    for path in candidates:
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    loaded = json.load(f)
                    if "orchestrator" in loaded:
                        return loaded
                    return {**DEFAULT_CONFIG, **loaded}
            except (json.JSONDecodeError, OSError):
                pass
    return dict(DEFAULT_CONFIG)


def _get_fs(cwd: str | None = None) -> AgentFileSystem:
    return AgentFileSystem(Path(cwd or _active_cwd) / AGENT_DIR)


def _get_store(fs: AgentFileSystem) -> StateStore:
    return StateStore(fs.db_path)


def _get_event_log(fs: AgentFileSystem) -> EventLog:
    return EventLog(fs.event_log_path)


def _resolve_run_id(args: argparse.Namespace, store: StateStore) -> str:
    run_id = getattr(args, "run_id", None)
    if run_id:
        return run_id
    run = store.get_active_run() or store.get_latest_run()
    if not run:
        print("활성 run이 없습니다. 'cmux-agent start'로 시작하세요.", file=sys.stderr)
        sys.exit(1)
    return run.run_id


def _event_time(event: dict) -> str:
    ts = str(event.get("ts", ""))
    return ts.split("T")[1].split(".")[0] if "T" in ts else ts


def _failure_events(event_log: EventLog, run_id: str) -> list[dict]:
    return [
        event
        for event in event_log.read_all(run_id)
        if event.get("event") in FAILURE_EVENTS
    ]


def _failed_artifacts(fs: AgentFileSystem) -> list[Path]:
    if not fs.failed.exists():
        return []
    return sorted(path for path in fs.failed.iterdir() if path.is_file())


def _failure_summary(fs: AgentFileSystem, event_log: EventLog, run_id: str) -> dict[str, int]:
    events = _failure_events(event_log, run_id)
    artifact_failures = sum(
        1 for event in events if event.get("event") == "artifact.validation_failed"
    )
    message_failures = sum(1 for event in events if event.get("event") == "message.failed")
    failed_files = len(_failed_artifacts(fs))
    return {
        "artifact": artifact_failures,
        "message": message_failures,
        "files": failed_files,
    }


def _print_failure_summary(
    fs: AgentFileSystem,
    event_log: EventLog,
    run_id: str,
) -> None:
    summary = _failure_summary(fs, event_log, run_id)
    if not any(summary.values()):
        return
    print(
        "\nFailures: "
        f"{summary['artifact']} artifact validation, "
        f"{summary['message']} message, "
        f"{summary['files']} failed artifacts"
    )
    print("Details: cmux-agent failures")


def _print_failure_details(
    fs: AgentFileSystem,
    event_log: EventLog,
    run_id: str,
    *,
    limit: int,
) -> None:
    events = _failure_events(event_log, run_id)[-limit:]
    failed_files = _failed_artifacts(fs)[-limit:]

    if not events and not failed_files:
        print("최근 실패가 없습니다.")
        return

    if events:
        print("Recent failure events:")
        for event in events:
            data = event.get("data") or {}
            reason = data.get("reason", "-")
            path = data.get("path")
            target = Path(str(path)).name if path else data.get("message_id", "-")
            print(f"  {_event_time(event)}  {event['event']:<28} {target} - {reason}")

    if failed_files:
        print("\nFailed artifacts:")
        for path in failed_files:
            print(f"  {path.name}")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _cmux_agent_paths() -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / "cmux-agent"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        paths.append(candidate)
    return paths


def _is_current_python_env_script(path: Path, executable: str | None = None) -> bool:
    executable_path = Path(executable or sys.executable)
    executable_parents = {executable_path.parent.resolve()}
    try:
        executable_parents.add(executable_path.resolve().parent)
    except OSError:
        pass
    try:
        script_path = path.resolve()
    except OSError:
        script_path = path
    script_parents = {path.parent.resolve(), script_path.parent}
    return bool(executable_parents & script_parents)


def _parse_help_commands(help_text: str) -> set[str]:
    commands: set[str] = set()
    for group in re.findall(r"\{([^{}]+)\}", help_text.replace("\n", "")):
        names = [part.strip() for part in group.split(",")]
        if any(name in names for name in ("doctor", "start", "task")):
            commands.update(name for name in names if name)
    return commands


def _probe_cmux_agent_commands(path: Path) -> set[str] | None:
    status = _probe_cmux_agent_script(path)
    return status["commands"]


# Probe outcomes for callers that want to distinguish failure modes.
PROBE_OK = "ok"
PROBE_MODULE_NOT_FOUND = "module_not_found"
PROBE_TIMEOUT = "timeout"
PROBE_OS_ERROR = "os_error"
PROBE_NONZERO_EXIT = "nonzero_exit"


def _probe_cmux_agent_script(path: Path) -> dict[str, object]:
    """Run ``<path> --help`` and report what happened.

    Returns a dict with:
    - ``status``: one of the PROBE_* constants
    - ``commands``: parsed command set when status == PROBE_OK, else None
    - ``stderr``: captured stderr (truncated)
    - ``returncode``: subprocess return code, or -1 when unavailable
    """
    try:
        proc = subprocess.run(
            [str(path), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": PROBE_TIMEOUT, "commands": None, "stderr": "", "returncode": -1}
    except OSError as exc:
        return {
            "status": PROBE_OS_ERROR,
            "commands": None,
            "stderr": str(exc),
            "returncode": -1,
        }

    stderr = (proc.stderr or "").strip()[:400]
    combined = proc.stdout + proc.stderr

    # Editable-install drift: .venv/bin/cmux-agent runs but its sys.path
    # cannot find the cmux_agent package. This shows up on Python 3.13 +
    # uv editable installs in script execution mode.
    if "ModuleNotFoundError" in combined and "cmux_agent" in combined:
        return {
            "status": PROBE_MODULE_NOT_FOUND,
            "commands": None,
            "stderr": stderr,
            "returncode": proc.returncode,
        }

    if proc.returncode != 0:
        return {
            "status": PROBE_NONZERO_EXIT,
            "commands": None,
            "stderr": stderr,
            "returncode": proc.returncode,
        }

    return {
        "status": PROBE_OK,
        "commands": _parse_help_commands(combined),
        "stderr": stderr,
        "returncode": proc.returncode,
    }


def _missing_supported_commands(commands: set[str] | None) -> list[str]:
    if commands is None:
        return []
    return [name for name in SUPPORTED_COMMANDS if name not in commands]


def cmd_doctor(_args: argparse.Namespace) -> None:
    cmux = CmuxAdapter()
    checks = []

    # Python
    v = sys.version.split()[0]
    checks.append(("python", True, v))

    # cmux
    if cmux.is_available():
        checks.append(("cmux", True, "설치됨"))
    else:
        checks.append(("cmux", False, "미설치 또는 미실행"))

    # cmux CLI
    cmux_bin = shutil.which("cmux")
    if cmux_bin:
        checks.append(("cmux CLI", True, cmux_bin))
    else:
        checks.append(("cmux CLI", False, "PATH에 없음"))

    # cmux-agent runtime / PATH drift
    module_root = Path(__file__).resolve().parents[1]
    checks.append(("cmux-agent module", True, str(module_root)))
    checks.append(("cmux-agent python", True, sys.executable))
    checks.append(("cmux-agent supported commands", True, ", ".join(SUPPORTED_COMMANDS)))

    agent_paths = _cmux_agent_paths()
    if not agent_paths:
        checks.append(("cmux-agent CLI", False, "PATH에 없음; python -m cmux_agent 사용 가능"))
    else:
        active_path = agent_paths[0]
        active_status = _probe_cmux_agent_script(active_path)
        active_commands = active_status["commands"]
        active_probe_status = active_status["status"]
        missing = _missing_supported_commands(active_commands)
        active_ok = (
            _is_current_python_env_script(active_path)
            and active_probe_status == PROBE_OK
            and not missing
        )
        if active_probe_status == PROBE_MODULE_NOT_FOUND:
            detail = (
                f"{active_path} (cmux_agent import 실패 — Python 3.13 + uv editable 설치 알려진 이슈; "
                "fallback: 'python -m cmux_agent' 또는 'uv tool install --force ./cmux-agent')"
            )
        elif active_probe_status == PROBE_TIMEOUT:
            detail = f"{active_path} (--help 5초 초과)"
        elif active_probe_status == PROBE_OS_ERROR:
            detail = f"{active_path} (실행 실패: {active_status['stderr']})"
        elif active_probe_status == PROBE_NONZERO_EXIT:
            detail = (
                f"{active_path} (exit {active_status['returncode']}: "
                f"{active_status['stderr'] or 'no stderr'})"
            )
        elif missing:
            detail = f"{active_path} (missing commands: {', '.join(missing)})"
        elif not _is_current_python_env_script(active_path):
            detail = f"{active_path} (현재 Python env와 다름)"
        else:
            detail = str(active_path)
        checks.append(("cmux-agent CLI", active_ok, detail))

        stale_paths = []
        for path in agent_paths[1:]:
            stale_status = _probe_cmux_agent_script(path)
            if stale_status["status"] == PROBE_MODULE_NOT_FOUND:
                stale_paths.append(f"{path} cmux_agent import 실패")
                continue
            missing = _missing_supported_commands(stale_status["commands"])
            if missing:
                stale_paths.append(f"{path} missing {', '.join(missing)}")
        if stale_paths:
            checks.append(("cmux-agent PATH drift", False, "; ".join(stale_paths)))

    # AI CLI
    for cli_name in ("claude", "codex", "gemini"):
        path = shutil.which(cli_name)
        if path:
            checks.append((cli_name, True, "설치됨"))
        else:
            checks.append((cli_name, False, "미설치"))

    for name, ok, detail in checks:
        mark = "\u2713" if ok else "\u2717"
        print(f"  {mark} {name}: {detail}")


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------

def _smoke_cwd(args: argparse.Namespace) -> tuple[Path, bool]:
    requested = getattr(args, "smoke_cwd", None) or getattr(args, "cwd", ".")
    if requested and requested != ".":
        path = Path(str(requested)).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path, False
    return Path(tempfile.mkdtemp(prefix="awf-cmux-smoke.")), True


def _write_smoke_spawn_artifact(
    fs: AgentFileSystem,
    *,
    worker_template: str,
    provider: str,
) -> Path:
    safe_template = re.sub(r"[^A-Za-z0-9_.-]+", "-", worker_template).strip("-") or "worker"
    path = fs.outbox / f"smoke-spawn-{safe_template}.json"
    tmp = path.with_suffix(".tmp")
    payload = {
        "type": "control",
        "sender": "orchestrator",
        "recipient": "controller",
        "message": f"Need isolated {worker_template} worker for cmux smoke verification",
        "action": "spawn_agent",
        "agent": {
            "template": worker_template,
            "provider": provider,
        },
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.rename(path)
    return path


def _find_smoke_spawn_result(fs: AgentFileSystem) -> dict | None:
    inbox = fs.inbox / "orchestrator"
    if not inbox.is_dir():
        return None
    for path in sorted(inbox.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        context = payload.get("context") or {}
        if context.get("control_action") == "spawn_agent":
            return payload
    return None


def _wait_for_smoke_spawn_result(
    fs: AgentFileSystem,
    store: StateStore,
    run_id: str,
    *,
    timeout: float,
    poll_interval: float,
) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() <= deadline:
        result = _find_smoke_spawn_result(fs)
        if result:
            agent = (result.get("context") or {}).get("agent") or {}
            name = str(agent.get("name") or "")
            if name and store.get_agent_by_name(run_id, name):
                return result
        if list(fs.failed.glob("*.json")):
            return None
        time.sleep(poll_interval)
    return None


def _cleanup_smoke(
    *,
    fs: AgentFileSystem,
    store: StateStore,
    event_log: EventLog,
    run: Run | None,
    workspace_id: str | None,
    cwd: Path,
    created_tmp: bool,
    keep: bool,
) -> None:
    if keep:
        print(f"Smoke artifacts kept: {cwd}")
        return

    if run:
        current = store.get_run(run.run_id)
        if current and current.status != RunStatus.COMPLETED:
            old = current.status.value
            store.update_run_status(run.run_id, RunStatus.COMPLETED)
            event_log.append(run_status_changed(run.run_id, old, "COMPLETED"))

    if workspace_id:
        result = CmuxAdapter().close_workspace(workspace_id)
        if result.ok:
            print(f"Closed workspace: {workspace_id}")
        else:
            print(f"Workspace close failed: {workspace_id} - {result.stderr}", file=sys.stderr)

    if fs.base.exists():
        shutil.rmtree(str(fs.base), ignore_errors=True)
        print(f"Cleaned: {fs.base}")
    if created_tmp and cwd.exists():
        shutil.rmtree(str(cwd), ignore_errors=True)
        print(f"Removed smoke cwd: {cwd}")


def cmd_smoke(args: argparse.Namespace) -> None:
    global _active_cwd  # noqa: PLW0603

    cwd, created_tmp = _smoke_cwd(args)
    template = getattr(args, "smoke_template", None) or getattr(args, "template", None) or "review"
    templates_dir = getattr(args, "smoke_templates_dir", None) or getattr(args, "templates_dir", None)
    worker_template = getattr(args, "worker_template", "test") or "test"
    provider = getattr(args, "provider", "codex") or "codex"
    timeout = float(getattr(args, "timeout", 20.0) or 20.0)
    poll_interval = float(getattr(args, "poll_interval", 0.5) or 0.5)
    keep = bool(getattr(args, "keep", False))

    previous_cwd = _active_cwd
    _active_cwd = str(cwd)
    fs = _get_fs(str(cwd))
    store = _get_store(fs)
    event_log = _get_event_log(fs)
    run: Run | None = None
    workspace_id: str | None = None

    print(f"Smoke cwd: {cwd}")
    print(f"Template: {template}")
    print(f"Dynamic worker template: {worker_template}")

    try:
        cmd_start(
            argparse.Namespace(
                cwd=str(cwd),
                template=template,
                templates_dir=templates_dir,
                attach_orchestrator=True,
            )
        )

        run = store.get_active_run()
        if not run:
            print("Smoke FAIL: active run not found", file=sys.stderr)
            sys.exit(1)
        workspace_id = run.workspace_id

        artifact = _write_smoke_spawn_artifact(
            fs,
            worker_template=worker_template,
            provider=provider,
        )
        print(f"Spawn artifact: {artifact}")

        result = _wait_for_smoke_spawn_result(
            fs,
            store,
            run.run_id,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        if not result:
            print("Smoke FAIL: dynamic worker spawn result not delivered", file=sys.stderr)
            _print_failure_details(fs, event_log, run.run_id, limit=10)
            sys.exit(1)

        agent = (result.get("context") or {}).get("agent") or {}
        worker_name = str(agent.get("name") or "")
        if worker_name:
            print(f"Spawned worker: {worker_name} ({agent.get('provider') or provider})")
        else:
            print("Smoke FAIL: spawn result missing worker name", file=sys.stderr)
            sys.exit(1)

        protocol = fs.base / f"{worker_name.upper()}.md"
        if not protocol.is_file():
            print(f"Smoke FAIL: protocol missing for {worker_name}", file=sys.stderr)
            sys.exit(1)
        print(f"Protocol: {protocol.name}")

        cmd_task(argparse.Namespace(request="Smoke attach orchestrator dispatch prompt"))
        print("Smoke PASS")
    finally:
        _cleanup_smoke(
            fs=fs,
            store=store,
            event_log=event_log,
            run=run,
            workspace_id=workspace_id,
            cwd=cwd,
            created_tmp=created_tmp,
            keep=keep,
        )
        _active_cwd = previous_cwd


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def _parse_surface_ref(output: str) -> str | None:
    """cmux new-surface 출력에서 surface ref를 추출한다.

    출력 형식: "OK surface:14 pane:13 workspace:5"
    """
    return parse_surface_ref(output)


def _parse_workspace_ref(output: str) -> str | None:
    """cmux new-workspace 출력에서 workspace ref를 추출한다.

    출력 형식: "OK workspace:5"
    """
    for token in output.split():
        if token.startswith("workspace:"):
            return token
    return None


def _watch_command(cwd: str) -> str:
    python = shlex.quote(sys.executable)
    return f"{python} -m cmux_agent --cwd {shlex.quote(cwd)} watch\n"


def cmd_start(args: argparse.Namespace) -> None:
    cwd = getattr(args, "cwd", ".")
    attach_orchestrator = bool(getattr(args, "attach_orchestrator", False))
    # --template 지정 시 템플릿을 직접 참조 (파일 복사 없음)
    global _active_template_dir  # noqa: PLW0603
    template_name = getattr(args, "template", None)
    templates_dir = getattr(args, "templates_dir", None)
    if template_name:
        template_dir = _resolve_template_dir(template_name, templates_dir)
        if template_dir:
            _active_template_dir = template_dir
            print(f"  ✓ 템플릿 참조: {template_name} ({template_dir})")
        else:
            _active_template_dir = None
            print(f"  ⚠ 템플릿 '{template_name}'을 찾을 수 없습니다.", file=sys.stderr)
    else:
        _active_template_dir = None
    fs = _get_fs(cwd)
    fs.init()
    if template_name and _active_template_dir:
        _write_template_state(fs, template_name, _active_template_dir)
    else:
        _clear_template_state(fs)
    store = _get_store(fs)
    event_log = _get_event_log(fs)
    cmux = CmuxAdapter()

    # run 생성
    run = Run()
    store.save_run(run)
    event_log.append(run_created(run.run_id))

    # cmux workspace 생성 → 첫 번째 탭(controller)이 자동 생성됨
    ws_result = cmux.new_workspace(cwd=cwd)
    if not ws_result.ok:
        print(f"workspace 생성 실패: {ws_result.stderr}", file=sys.stderr)
        sys.exit(1)

    ws_ref = _parse_workspace_ref(ws_result.stdout)
    run.workspace_id = ws_ref
    store.save_run(run)
    time.sleep(0.3)

    # 첫 번째 탭(controller)의 surface ref 확인
    ctrl_surface = None
    tree_result = cmux.tree(workspace_id=ws_ref)
    if tree_result.ok:
        try:
            tree = tree_result.json()
            for w in tree.get("windows", []):
                for ws in w.get("workspaces", []):
                    if ws.get("ref") == ws_ref:
                        for pane in ws.get("panes", []):
                            for s in pane.get("surfaces", []):
                                ctrl_surface = s.get("ref")
                                break
        except (json.JSONDecodeError, KeyError):
            pass

    # 설정 파일에서 탭 구성을 동적으로 결정
    config = _load_config(cwd)
    tabs: list[tuple[str, AgentRole, str | None]] = [
        ("controller", AgentRole.CONTROLLER, ctrl_surface),
        ("orchestrator", AgentRole.ORCHESTRATOR, None),
    ]
    for name in sorted(config):
        if name.startswith("worker-"):
            tabs.append((name, AgentRole.WORKER, None))

    # orchestrator, worker 탭 생성. attach 모드에서는 현재 세션이
    # orchestrator 역할을 하므로 별도 orchestrator surface를 만들지 않는다.
    for i in range(1, len(tabs)):
        if attach_orchestrator and tabs[i][1] == AgentRole.ORCHESTRATOR:
            continue
        result = cmux.new_surface(workspace_id=ws_ref)
        if result.ok:
            tabs[i] = (tabs[i][0], tabs[i][1], _parse_surface_ref(result.stdout))
        time.sleep(0.2)

    # agent 등록 및 탭 이름 설정
    agents = []
    for name, role, surface_id in tabs:
        agent = Agent(
            run_id=run.run_id,
            role=role,
            name=name,
            surface_id=surface_id,
        )
        store.save_agent(agent)
        fs.create_inbox(name)
        event_log.append(agent_registered(run.run_id, name, role.value))
        if surface_id:
            cmux.rename_tab(name, surface_id=surface_id, workspace_id=ws_ref)
        agents.append(agent)

    # 프로토콜 파일 생성
    prompt_builder = PromptBuilder(str(fs.outbox), str(fs.inbox))
    prompt_builder.write_protocol_files(fs.base, agents, template_dir=_active_template_dir)

    # run 상태 → RUNNING
    run.transition_to(RunStatus.RUNNING)
    store.save_run(run)
    event_log.append(run_status_changed(run.run_id, "CREATED", "RUNNING"))

    # controller 탭에서 watch 자동 실행
    controller = agents[0]
    if controller.surface_id:
        cmux.send_text(
            _watch_command(cwd),
            surface_id=controller.surface_id,
            workspace_id=ws_ref,
        )

    # orchestrator, worker 탭에서 AI CLI 자동 실행 (설정 파일 기반)
    config = _load_config(cwd)
    time.sleep(0.5)
    for agent in agents:
        if agent.role in (AgentRole.ORCHESTRATOR, AgentRole.WORKER) and agent.surface_id:
            entry = _normalize_agent_entry(config.get(agent.name, "claude"))
            provider = entry.get("provider", "claude")
            flags = entry.get("flags", "")
            command = provider_command(provider, flags)
            cmux.send_text(
                f"{command}\n",
                surface_id=agent.surface_id,
                workspace_id=ws_ref,
            )
            cmux.send_text(
                prompt_builder.build_startup_prompt(agent),
                surface_id=agent.surface_id,
                workspace_id=ws_ref,
            )
            cmux.send_key(
                "enter",
                surface_id=agent.surface_id,
                workspace_id=ws_ref,
            )
            time.sleep(0.3)

    cmux.notify(title="cmux-agent", body=f"Run 시작: {run.run_id[:8]}")
    cmux.log(f"run started: {run.run_id[:8]}", level="success", source="cmux-agent")

    print(f"Run: {run.run_id}")
    for a in agents:
        print(f"  {a.name:<16} {a.surface_id or '-'}")
    print(f"Workspace: {ws_ref}")
    if attach_orchestrator:
        print("\n현재 세션이 orchestrator입니다.")
        print(f"  1. {fs.base / 'ORCHESTRATOR-COMMON.md'} 를 읽으세요.")
        print(f"  2. {fs.base / 'ORCHESTRATOR.md'} 를 읽으세요.")
        print(f"  3. dispatch/control artifact를 {fs.outbox} 에 작성하세요.")
        print(f"  4. worker 결과는 {fs.inbox / 'orchestrator'} 와 events/failures 명령으로 확인하세요.")
    print(f"\n'cmux-agent task \"요청\"' 으로 작업을 시작하세요.")


# ---------------------------------------------------------------------------
# task
# ---------------------------------------------------------------------------

def cmd_task(args: argparse.Namespace) -> None:
    fs = _get_fs()
    store = _get_store(fs)
    cmux = CmuxAdapter()

    run = store.get_active_run()
    if not run:
        print("활성 run이 없습니다. 'cmux-agent' 로 시작하세요.", file=sys.stderr)
        sys.exit(1)

    orch = store.get_agent_by_name(run.run_id, "orchestrator")
    if not orch:
        print("orchestrator가 등록되지 않았습니다.", file=sys.stderr)
        sys.exit(1)

    request = args.request

    workers = store.get_agents(run.run_id)
    active_names = [
        a.name for a in workers
        if a.role == AgentRole.WORKER
        and a.surface_id
        and cmux.is_surface_alive(a.surface_id)
    ]
    prompt = (
        f"{request}\n"
        f"\n"
        f"위 작업을 분석하고, worker에게 위임하세요.\n"
        f"작업 크기상 worker가 더 필요하거나 현재 worker가 부족하면 control spawn_agent artifact를 먼저 생성하세요.\n"
        f"{fs.outbox} 에 dispatch artifact(JSON)를 생성하세요.\n"
        f"사용 가능한 worker: {', '.join(active_names) if active_names else '(없음 - 필요 시 spawn_agent 요청)'}"
    )

    if not orch.surface_id:
        print("현재 세션 attached orchestrator 모드입니다.")
        print("아래 요청을 기준으로 dispatch/control artifact를 작성하세요.\n")
        print(prompt)
        return

    cmux.send_text(prompt, surface_id=orch.surface_id, workspace_id=run.workspace_id)
    cmux.send_key("enter", surface_id=orch.surface_id, workspace_id=run.workspace_id)

    cmux.log(f"task → orchestrator: {request[:50]}", level="info", source="cmux-agent")
    print(f"작업 주입 완료: orchestrator ({orch.surface_id})")


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def cmd_stop(args: argparse.Namespace) -> None:
    fs = _get_fs()
    store = _get_store(fs)
    event_log = _get_event_log(fs)
    run_id = _resolve_run_id(args, store)

    run = store.get_run(run_id)
    if not run:
        print(f"Run을 찾을 수 없습니다: {run_id}", file=sys.stderr)
        sys.exit(1)

    old = run.status.value
    store.update_run_status(run_id, RunStatus.COMPLETED)
    event_log.append(run_status_changed(run_id, old, "COMPLETED"))
    print(f"Run 종료: {run_id}")

    # 런타임 디렉토리 정리 (프로젝트 오염 방지)
    if fs.base.exists() and getattr(args, "clean", True):
        shutil.rmtree(str(fs.base), ignore_errors=True)
        print(f"  ✓ {fs.base} 정리 완료")


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------

def cmd_register(args: argparse.Namespace) -> None:
    fs = _get_fs()
    store = _get_store(fs)
    event_log = _get_event_log(fs)
    run = store.get_active_run()
    if not run:
        print("활성 run이 없습니다.", file=sys.stderr)
        sys.exit(1)

    role = AgentRole.ORCHESTRATOR if args.role == "orchestrator" else AgentRole.WORKER

    if store.get_agent_by_name(run.run_id, args.name):
        print(f"이미 등록된 agent: {args.name}", file=sys.stderr)
        sys.exit(1)

    agent = Agent(
        run_id=run.run_id,
        role=role,
        name=args.name,
        surface_id=getattr(args, "surface_id", None),
    )
    store.save_agent(agent)
    fs.create_inbox(args.name)
    event_log.append(agent_registered(run.run_id, args.name, role.value))
    print(f"Agent 등록: {args.name} ({role.value})")


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------

def cmd_spawn(args: argparse.Namespace) -> None:
    fs = _get_fs()
    template_dir = _restore_template_dir(fs)
    store = _get_store(fs)
    event_log = _get_event_log(fs)
    cmux = CmuxAdapter()

    run = store.get_active_run()
    if not run:
        print("활성 run이 없습니다.", file=sys.stderr)
        sys.exit(1)

    prompt_builder = PromptBuilder(str(fs.outbox), str(fs.inbox))
    runtime = AgentRuntime(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=prompt_builder,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        template_dir=template_dir,
        provider_config=_load_config(_active_cwd),
    )
    result = runtime.spawn_worker(
        name=args.name,
        role=getattr(args, "role", None),
        template=getattr(args, "worker_template", None),
        provider=args.provider,
        flags=args.flags,
    )
    if not result.ok:
        print(f"worker 생성 실패: {result.error}", file=sys.stderr)
        sys.exit(1)

    print(f"Worker 생성: {result.name} ({result.provider}) {result.surface_id or '-'}")


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

def cmd_agents(args: argparse.Namespace) -> None:
    fs = _get_fs()
    store = _get_store(fs)
    run_id = _resolve_run_id(args, store)

    agents = store.get_agents(run_id)
    if not agents:
        print("등록된 agent가 없습니다.")
        return

    print(f"Run: {run_id[:8]}...\n")
    for a in agents:
        sid = a.surface_id or "-"
        print(f"  {a.name:<16} | {a.role.value:<14} | surface: {sid}")


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------

def cmd_watch(args: argparse.Namespace) -> None:
    # watchdog 스레드에서도 즉시 출력되도록 flush 핸들러 사용
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.INFO)

    fs = _get_fs()
    template_dir = _restore_template_dir(fs)
    store = _get_store(fs)
    event_log = _get_event_log(fs)
    cmux = CmuxAdapter()

    run = store.get_active_run()
    if not run:
        print("활성 run이 없습니다.", file=sys.stderr)
        sys.exit(1)

    prompt_builder = PromptBuilder(
        outbox_path=str(fs.outbox),
        inbox_base=str(fs.inbox),
    )
    runtime = AgentRuntime(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=prompt_builder,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        template_dir=template_dir,
        provider_config=_load_config(_active_cwd),
    )
    broker = MessageBroker(
        store=store,
        event_log=event_log,
        fs=fs,
        cmux=cmux,
        prompt_builder=prompt_builder,
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        runtime=runtime,
    )
    watcher = ArtifactWatcher(fs.outbox, broker)

    print(f"Watcher 시작: {fs.outbox}")
    print("Ctrl+C로 중지")

    cmux.set_status("watcher", "active", icon="eye", color="#00CC00")

    if getattr(args, "daemon", False):
        watcher.start_background()
        print("백그라운드 모드로 실행 중...")
        try:
            import signal
            signal.pause()
        except KeyboardInterrupt:
            pass
    else:
        watcher.start()

    cmux.set_status("watcher", "stopped", color="#CC0000")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> None:
    fs = _get_fs()
    store = _get_store(fs)
    event_log = _get_event_log(fs)
    run_id = _resolve_run_id(args, store)

    run = store.get_run(run_id)
    if not run:
        print(f"Run을 찾을 수 없습니다: {run_id}", file=sys.stderr)
        sys.exit(1)

    agents = store.get_agents(run_id)
    counts = store.count_messages(run_id)

    print(f"Run: {run.run_id}")
    print(f"Status: {run.status.value}")
    print(f"Workspace: {run.workspace_id or '-'}")
    print(f"Created: {run.created_at.strftime('%Y-%m-%d %H:%M:%S')}")

    if agents:
        print(f"\nAgents:")
        for a in agents:
            print(f"  {a.name:<16} | {a.role.value}")

    if counts:
        parts = [f"{count} {status.lower()}" for status, count in counts.items()]
        print(f"\nMessages: {', '.join(parts)}")

    _print_failure_summary(fs, event_log, run_id)
    if getattr(args, "failures", False):
        limit = getattr(args, "failure_limit", 10)
        print()
        _print_failure_details(fs, event_log, run_id, limit=limit)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

def cmd_events(args: argparse.Namespace) -> None:
    fs = _get_fs()
    store = _get_store(fs)
    event_log = _get_event_log(fs)
    run_id = _resolve_run_id(args, store)

    events = event_log.read_all(run_id)
    if getattr(args, "failures", False):
        events = [event for event in events if event.get("event") in FAILURE_EVENTS]
    limit = getattr(args, "limit", 20)
    events = events[-limit:]

    if not events:
        print("이벤트가 없습니다.")
        return

    for e in events:
        ts = _event_time(e)
        data_str = ""
        if e.get("data"):
            parts = [f"{k}={v}" for k, v in e["data"].items()]
            data_str = " ".join(parts)
        print(f"  {ts}  {e['event']:<30} {data_str}")


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------

def cmd_failures(args: argparse.Namespace) -> None:
    fs = _get_fs()
    store = _get_store(fs)
    event_log = _get_event_log(fs)
    run_id = _resolve_run_id(args, store)

    _print_failure_details(
        fs,
        event_log,
        run_id,
        limit=getattr(args, "limit", 10),
    )


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

def cmd_send(args: argparse.Namespace) -> None:
    fs = _get_fs()
    store = _get_store(fs)

    run = store.get_active_run()
    if not run:
        print("활성 run이 없습니다.", file=sys.stderr)
        sys.exit(1)

    agent = store.get_agent_by_name(run.run_id, args.recipient)
    if not agent:
        print(f"미등록 agent: {args.recipient}", file=sys.stderr)
        sys.exit(1)

    # outbox에 artifact로 작성 (수동 전송)
    artifact = {
        "type": "dispatch",
        "sender": "controller",
        "recipient": args.recipient,
        "message": args.message,
    }
    artifact_name = f"{int(time.time())}-controller-dispatch.json"
    artifact_path = fs.outbox / artifact_name
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"메시지 전송 (outbox): {artifact_name}")
    print("watcher가 실행 중이면 자동으로 라우팅됩니다.")


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------

def cmd_messages(args: argparse.Namespace) -> None:
    fs = _get_fs()
    store = _get_store(fs)
    run_id = _resolve_run_id(args, store)

    messages = store.get_messages(run_id)
    if not messages:
        print("메시지가 없습니다.")
        return

    for m in messages:
        ts = m.created_at.strftime("%H:%M:%S")
        arrow = "\u2192"
        status_mark = {
            "PENDING": "\u23f3",
            "DELIVERED": "\u2705",
            "FAILED": "\u274c",
        }.get(m.status.value, "?")
        print(
            f"  {status_mark} {ts}  {m.sender} {arrow} {m.recipient}"
            f"  [{m.type.value}] {m.status.value}"
        )
