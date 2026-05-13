"""메시지 브로커 — artifact를 파싱하고 수신자 inbox에 전달한다."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path

from cmux_agent.application.prompting import PromptBuilder
from cmux_agent.application.runtime import AgentRuntime
from cmux_agent.domain.events import (
    artifact_detected,
    artifact_validation_failed,
    message_delivered,
    message_failed,
)
from cmux_agent.domain.models import Message, MessageStatus, MessageType
from cmux_agent.infrastructure.cmux import CmuxAdapter
from cmux_agent.infrastructure.event_log import EventLog
from cmux_agent.infrastructure.filesystem import AgentFileSystem
from cmux_agent.infrastructure.storage import StateStore

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

# Branches workers must not commit directly to. Exact match or glob-like prefix.
# 2026-05-13 BLIP Gem cycle §3.3 incident: worker pushed feat commits straight to
# `main` on a sibling repo, triggering Argo CD's prod manifest auto-update.
FORBIDDEN_BRANCH_PATTERNS = (
    re.compile(r"^main$"),
    re.compile(r"^master$"),
    re.compile(r"^production$"),
    re.compile(r"^prod$"),
    re.compile(r"^release(/|-).*"),
    re.compile(r"^prod(/|-).*"),
)


def _is_forbidden_branch(name: str) -> bool:
    return any(p.match(name) for p in FORBIDDEN_BRANCH_PATTERNS)


class MessageBroker:
    """artifact를 수신하여 라우팅하고 inbox에 전달하는 메시지 브로커."""

    def __init__(
        self,
        store: StateStore,
        event_log: EventLog,
        fs: AgentFileSystem,
        cmux: CmuxAdapter,
        prompt_builder: PromptBuilder,
        run_id: str,
        workspace_id: str | None = None,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self._store = store
        self._event_log = event_log
        self._fs = fs
        self._cmux = cmux
        self._prompt = prompt_builder
        self._run_id = run_id
        self._workspace_id = workspace_id
        self._runtime = runtime

    # -- ArtifactConsumer 프로토콜 구현 ------------------------------------

    def handle_artifact(self, artifact_path: Path, data: dict) -> None:
        """watcher가 감지한 artifact를 처리한다."""
        # 검증 실패 artifact 처리
        if "_error" in data:
            self._event_log.append(
                artifact_validation_failed(self._run_id, str(artifact_path), data["_error"])
            )
            self._fs.move_to_failed(artifact_path)
            return

        sender = data["sender"]
        recipient = data["recipient"]
        msg_type_str = data["type"]

        print(f"  → 라우팅: {sender} → {recipient} ({msg_type_str})", flush=True)

        self._event_log.append(
            artifact_detected(self._run_id, str(artifact_path), sender)
        )

        # sender 확인
        if not self._store.get_agent_by_name(self._run_id, sender):
            logger.warning("미등록 송신자: %s", sender)
            self._event_log.append(
                artifact_validation_failed(
                    self._run_id, str(artifact_path), f"미등록 sender: {sender}"
                )
            )
            self._fs.move_to_failed(artifact_path)
            return

        if msg_type_str == "control":
            self._handle_control(sender=sender, artifact_path=artifact_path, payload=data)
            return

        # recipient 확인
        recipient_agent = self._store.get_agent_by_name(self._run_id, recipient)
        if not recipient_agent:
            logger.warning("미등록 수신자: %s", recipient)
            self._event_log.append(
                artifact_validation_failed(
                    self._run_id, str(artifact_path), f"미등록 recipient: {recipient}"
                )
            )
            self._fs.move_to_failed(artifact_path)
            return

        # recipient surface 생존 확인
        if recipient_agent.surface_id and not self._cmux.is_surface_alive(recipient_agent.surface_id):
            print(f"  ✗ 비활성 수신자: {recipient} (탭 닫힘)", flush=True)
            self._event_log.append(
                artifact_validation_failed(
                    self._run_id, str(artifact_path), f"비활성 recipient: {recipient}"
                )
            )
            self._fs.move_to_failed(artifact_path)
            return

        self._route_message(
            sender=sender,
            recipient=recipient,
            msg_type=MessageType(msg_type_str.upper()),
            payload=data,
            artifact_path=artifact_path,
        )

    # -- 내부 라우팅 --------------------------------------------------------

    def _handle_control(self, *, sender: str, artifact_path: Path, payload: dict) -> None:
        action = str(payload.get("action", "") or "").strip()
        if action != "spawn_agent":
            self._event_log.append(
                artifact_validation_failed(
                    self._run_id, str(artifact_path), f"지원하지 않는 control action: {action or '-'}"
                )
            )
            self._fs.move_to_failed(artifact_path)
            return

        if self._runtime is None:
            self._event_log.append(
                artifact_validation_failed(
                    self._run_id, str(artifact_path), "runtime spawner unavailable"
                )
            )
            self._fs.move_to_failed(artifact_path)
            return

        agent_data = payload.get("agent")
        if not isinstance(agent_data, dict):
            agent_data = {}
        result = self._runtime.spawn_worker(
            name=str(agent_data.get("name") or payload.get("agent_name") or "").strip() or None,
            role=str(agent_data.get("role") or payload.get("role") or "").strip() or None,
            template=str(agent_data.get("template") or payload.get("template") or "").strip() or None,
            provider=str(agent_data.get("provider") or payload.get("provider") or "").strip() or None,
            flags=str(agent_data.get("flags") or payload.get("flags") or "").strip() or None,
        )

        if not result.ok:
            self._event_log.append(
                artifact_validation_failed(
                    self._run_id, str(artifact_path), result.error or "spawn_agent failed"
                )
            )
            self._fs.move_to_failed(artifact_path)
            return

        response = {
            "type": "result",
            "sender": "controller",
            "recipient": sender,
            "message": f"spawned {result.name} ({result.provider})",
            "context": {
                "control_action": "spawn_agent",
                "agent": {
                    "name": result.name,
                    "provider": result.provider,
                    "surface_id": result.surface_id,
                },
            },
        }
        self._route_message(
            sender="controller",
            recipient=sender,
            msg_type=MessageType.RESULT,
            payload=response,
            artifact_path=artifact_path,
        )

    def _route_message(
        self,
        *,
        sender: str,
        recipient: str,
        msg_type: MessageType,
        payload: dict,
        artifact_path: Path,
    ) -> None:
        msg = Message(
            run_id=self._run_id,
            sender=sender,
            recipient=recipient,
            type=msg_type,
            payload=json.dumps(payload, ensure_ascii=False),
            artifact_path=str(artifact_path),
        )
        self._store.save_message(msg)

        # delivery 메시지 구성
        delivery = self._prompt.build_delivery(
            sender=sender,
            recipient=recipient,
            msg_type=msg_type,
            payload=payload,
        )

        # inbox에 전달 (재시도 포함)
        delivered = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._fs.write_to_inbox(recipient, msg.message_id, delivery)
                delivered = True
                break
            except OSError:
                logger.warning(
                    "inbox 전달 실패 (시도 %d/%d): %s", attempt, MAX_RETRIES, recipient
                )

        if delivered:
            msg.mark_delivered()
            self._store.save_message(msg)
            self._event_log.append(
                message_delivered(self._run_id, msg.message_id, recipient)
            )
            print(f"  ✓ 전달 완료: {sender} → {recipient}", flush=True)
            self._inject_and_notify(recipient, sender, msg_type, payload)
        else:
            msg.mark_failed()
            self._store.save_message(msg)
            self._event_log.append(
                message_failed(self._run_id, msg.message_id, "inbox 전달 실패")
            )
            print(f"  ✗ 전달 실패: {sender} → {recipient}", flush=True)

        # 처리 완료된 artifact 이동
        try:
            self._fs.move_to_processed(artifact_path)
        except OSError:
            logger.warning("artifact 이동 실패: %s", artifact_path)

    # ---------- AI CLI idle/busy 검출 헬퍼 -------------------------------------

    # NOTE: claude code uses `⏺` as a prefix for normal output lines (not a busy spinner).
    # Do NOT include it here — false-positive busy detection causes infinite idle-wait.
    # Only include actual processing/spinner indicators.
    _BUSY_PATTERNS = (
        "esc to interrupt",
        "✶", "✻", "✽", "✢", "✺",
    )

    # Status verbs are only treated as busy when they appear together with a spinner glyph or
    # with the typical claude-code suffix like `for Xs` / `…`. We check via _is_busy.
    _BUSY_VERBS = (
        "Crunched", "Sketching", "Galloping", "Bloviating", "Sautéed",
        "Cascading", "Churned", "Cooking", "Grooving", "Thinking",
        "Hatching", "Pondering", "Tinkering", "Brewing", "Computing",
        "Working", "Gallivanting",
    )

    _STUCK_PATTERNS = (
        "[Pasted text",
        "Tab to amend",
        "Esc to cancel",
    )

    _PERMISSION_PATTERNS = (
        "Do you want to",
        "Yes, allow all edits",
        "Yes, and don't ask again",
    )

    def _read_surface(self, surface_id: str, lines: int = 30) -> str:
        """surface 화면을 read-screen으로 캡쳐. 실패 시 빈 문자열."""
        result = self._cmux.read_screen(
            surface_id=surface_id,
            workspace_id=self._workspace_id,
            lines=lines,
        )
        return result.stdout if result.ok else ""

    def _is_busy(self, surface_id: str) -> bool:
        """surface가 작업 중이면 True.

        검출 신호 (마지막 8줄만 검사 — 위쪽 scrollback 잔재로 false-positive 회피):
        - `esc to interrupt` 라인 (claude 진행 시 항상 표시)
        - spinner glyph (`✶ ✻ ✽ ✢ ✺`)
        - verb + `…` 또는 `for Xs` 형태 (`Crunched for 22s`, `Cascading…`)
        """
        screen = self._read_surface(surface_id, lines=20)
        if not screen:
            return False
        tail = "\n".join(screen.splitlines()[-8:])
        if any(p in tail for p in self._BUSY_PATTERNS):
            return True
        for verb in self._BUSY_VERBS:
            if f"{verb}…" in tail or f"{verb} for" in tail:
                return True
        return False

    def _wait_for_idle(self, surface_id: str, max_wait: float = 120.0, poll: float = 2.0) -> bool:
        """AI CLI 작업이 끝나 idle 될 때까지 대기. timeout 시 False."""
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if not self._is_busy(surface_id):
                return True
            time.sleep(poll)
        return False

    def _input_stuck(self, surface_id: str) -> bool:
        """enter 후에도 input box에 텍스트가 남아 stuck인지 검사.

        - busy patterns 있으면 정상 submit (return False).
        - stuck patterns ([Pasted text], Tab to amend 등)이 있거나
          input mode hint (`accept edits on`, `? for shortcuts`)가 있는데
          input line이 비어있지 않으면 stuck.
        """
        screen = self._read_surface(surface_id, lines=30)
        if not screen:
            return False
        if any(p in screen for p in self._BUSY_PATTERNS):
            return False
        if any(p in screen for p in self._STUCK_PATTERNS):
            return True
        # input hint 있고 마지막 input line이 비어있지 않으면 stuck
        if any(h in screen for h in ("accept edits on", "? for shortcuts", "shift+tab to cycle")):
            for line in reversed(screen.splitlines()):
                stripped = line.strip()
                if stripped.startswith("❯") and len(stripped) > 3:
                    return True
                if stripped.startswith("❯"):
                    return False
        return False

    def _detect_permission_dialog(self, surface_id: str) -> bool:
        """claude code 권한 dialog (Do you want to ... 1. Yes / 2. ... / 3. No)가 표시됐는지."""
        screen = self._read_surface(surface_id, lines=25)
        if not screen:
            return False
        return any(p in screen for p in self._PERMISSION_PATTERNS)

    def _approve_permission_dialog(self, surface_id: str) -> None:
        """권한 dialog 검출 시 명시적으로 option 1 (Yes) 선택.

        dialog selector mode에서 send_text "1"은 number-key처럼 처리되어 1번을 강제.
        그 후 enter로 confirm. shift+tab cycle로 default가 변동되어도 안전.
        """
        self._cmux.send_text("1", surface_id=surface_id, workspace_id=self._workspace_id)
        time.sleep(0.4)
        self._cmux.send_key("enter", surface_id=surface_id, workspace_id=self._workspace_id)
        time.sleep(0.6)
        logger.info("권한 dialog auto-approved (option 1 Yes) on surface=%s", surface_id)
        print(f"  ⚙ 권한 dialog auto-approved: {surface_id}", flush=True)

    def _drain_permission_dialogs(self, surface_id: str, max_iterations: int = 5) -> None:
        """연속된 권한 dialog를 최대 N회 자동 처리."""
        for _ in range(max_iterations):
            if not self._detect_permission_dialog(surface_id):
                return
            self._approve_permission_dialog(surface_id)

    def _send_with_verification(
        self,
        surface_id: str,
        text: str,
        max_retries: int = 3,
    ) -> bool:
        """send_text + send_key(enter) + 검증 + retry + 권한 dialog 자동 처리.

        race로 stuck 시 추가 enter 보냄. 권한 dialog 발생 시 option 1 (Yes) 자동 선택.
        최대 max_retries회.
        """
        self._cmux.send_text(text, surface_id=surface_id, workspace_id=self._workspace_id)
        # 1000자당 1초, 최소 1.5초 대기 (긴 paste mode finalize 포함)
        time.sleep(max(1.5, len(text) / 1000.0))
        self._cmux.send_key("enter", surface_id=surface_id, workspace_id=self._workspace_id)

        for attempt in range(max_retries):
            time.sleep(2.5)
            if self._detect_permission_dialog(surface_id):
                self._drain_permission_dialogs(surface_id)
                continue
            if not self._input_stuck(surface_id):
                return True
            logger.warning(
                "Dispatch stuck on surface=%s after attempt=%d, sending extra enter",
                surface_id, attempt + 1,
            )
            self._cmux.send_key("enter", surface_id=surface_id, workspace_id=self._workspace_id)
        if self._detect_permission_dialog(surface_id):
            self._drain_permission_dialogs(surface_id)
        return not self._input_stuck(surface_id) and not self._detect_permission_dialog(surface_id)

    def _current_branch(self, cwd: Path) -> str | None:
        """cycle root cwd의 현재 git branch. git repo 아니거나 detached/error 시 None."""
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        name = result.stdout.strip()
        return name if name and name != "HEAD" else None

    def _active_workflow_context(self) -> dict | None:
        """cycle root의 .workflow/state.json을 읽어 active workflow 정보를 반환.

        없거나 파싱 실패면 None. 있으면 {"id", "currentPhase", "phase_status"} dict.
        §1.7 incident 대응: cmux-agent broker가 작업 진행하는 동안 state.json이
        stale인 채로 멈춰서 cycle 진행도 추적이 불가능했다. 본 함수는 dispatch
        시점에 phase context를 worker prompt에 주입할 수 있게 정보 추출만 한다.
        """
        state_path = self._fs.base.parent / ".workflow" / "state.json"
        if not state_path.is_file():
            return None
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        current_phase = state.get("currentPhase")
        if not current_phase:
            return None
        phases = state.get("phases") or {}
        phase_meta = phases.get(current_phase) or {}
        return {
            "id": state.get("id", "unknown"),
            "currentPhase": str(current_phase),
            "phase_status": str(phase_meta.get("status", "unknown")),
            "state_path": state_path,
        }

    _APPLY_RESULT_SUPPORTED_PHASES = {"review", "verify", "impl", "test"}

    def _workflow_state_hint(self) -> str:
        """active workflow가 있으면 dispatch prompt에 추가할 state 갱신 안내.

        review/verify/impl/test phase는 apply-result로 자동 처리 가능하므로
        worker가 `result_file` + `phase` 필드를 포함한 result artifact를 발행하면
        broker가 _maybe_auto_apply_result로 awf wf apply-result를 호출한다 (§1.7 hard hook).
        plan/approve/done 등은 broker가 자동 처리 못 하므로 수동 안내 유지.
        """
        ctx = self._active_workflow_context()
        if not ctx:
            return ""
        phase = ctx["currentPhase"]
        cycle_root = self._fs.base.parent
        state_path = ctx["state_path"]
        if phase in self._APPLY_RESULT_SUPPORTED_PHASES:
            return (
                "\n📂 active workflow detected: "
                f"id={ctx['id']} phase={phase} status={ctx['phase_status']}\n"
                f"   완료 시 결과 JSON을 cycle root에 저장하고 result artifact의 "
                f"`result_file` + `phase` 필드에 경로/단계를 명시하세요. "
                f"broker가 `awf wf apply-result --phase {phase} --result-file <path>` "
                f"(cwd={cycle_root})를 자동 실행하여 state.json을 갱신합니다.\n"
            )
        return (
            "\n📂 active workflow detected: "
            f"id={ctx['id']} phase={phase} status={ctx['phase_status']}\n"
            f"   현재 phase '{phase}'는 awf wf apply-result 미지원이므로 "
            f"작업 완료 후 {state_path}에 진행 상황을 수동으로 반영하세요 "
            "(phases[phase].status, history 추가). "
            "stale state는 cycle 추적을 방해합니다.\n"
        )

    def _maybe_auto_apply_result(self, payload: dict) -> None:
        """result artifact의 `result_file` + `phase`로 apply-result subprocess 호출.

        §1.7 hard hook — 두 가지 모두 충족 시에만 동작 (no-op otherwise):
        - `.workflow/state.json`이 있어 active workflow context 검출됨
        - payload에 `result_file` (cycle root 상대/절대 경로) + `phase`
          (review/verify/impl/test 중 하나)가 모두 존재

        실패해도 dispatch 흐름 자체는 깨뜨리지 않는다 (logger.warning + early return).
        """
        ctx = self._active_workflow_context()
        if not ctx:
            return
        result_file = payload.get("result_file")
        phase = payload.get("phase") or ctx.get("currentPhase")
        if not isinstance(result_file, str) or not isinstance(phase, str):
            return
        if phase not in self._APPLY_RESULT_SUPPORTED_PHASES:
            logger.info("apply-result skip: phase %s not in supported set", phase)
            return
        cycle_root = self._fs.base.parent
        candidate = Path(result_file)
        if not candidate.is_absolute():
            candidate = cycle_root / candidate
        if not candidate.is_file():
            logger.warning("apply-result skip: result_file not found at %s", candidate)
            return
        try:
            completed = subprocess.run(
                ["awf", "wf", "apply-result", phase, str(candidate)],
                cwd=str(cycle_root),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("apply-result subprocess failed: %s", exc)
            return
        if completed.returncode == 0:
            logger.info("apply-result %s applied (file=%s)", phase, candidate)
            print(f"  ⚙ awf wf apply-result {phase} OK ({candidate.name})", flush=True)
        else:
            stderr = (completed.stderr or "").strip()[:400]
            logger.warning("apply-result %s returned %d: %s", phase, completed.returncode, stderr)
            print(f"  ⚠ awf wf apply-result {phase} returned {completed.returncode}", flush=True)

    def _branch_safety_warning(self) -> str:
        """forbidden branch 위에 있으면 dispatch 프롬프트에 prepend할 경고 문자열을 반환.

        §3.3 incident 재발 방지: worker가 base branch 확인 없이 main에 직커밋.
        broker는 cycle root cwd만 알 수 있고, sibling repo는 못 보므로 hard block
        대신 명시적 경고를 주입한다. multi-repo cycle에서도 work tree가 main이면
        sibling도 main인 경우가 많아 실용적으로 유용하다.
        """
        cycle_root = self._fs.base.parent
        branch = self._current_branch(cycle_root)
        if branch and _is_forbidden_branch(branch):
            return (
                f"\n⚠️  CRITICAL — base branch protection (cycle root: {cycle_root}):\n"
                f"   현재 branch = `{branch}` (forbidden). "
                f"이 cycle의 모든 git commit은 feature branch에서만 수행하세요.\n"
                f"   작업 시작 전 각 대상 repo에서 `git branch --show-current`로 확인하고, "
                f"main/master/production 이면 새 feat/* branch를 만들어 checkout 후 진행하세요.\n"
            )
        # branch 모르면 일반 안내 (정상적 multi-repo 운영 대비 보조 reminder)
        return (
            "\n📌 base branch 안내: 각 대상 repo에서 작업 시작 전 "
            "`git branch --show-current`를 확인하고, main/master/production 직커밋은 금지입니다.\n"
        )

    def _inject_and_notify(
        self,
        recipient: str,
        sender: str,
        msg_type: MessageType,
        payload: dict,
    ) -> None:
        """AI CLI 터미널에 메시지를 자동 주입하고 cmux 알림을 보낸다."""
        agent = self._store.get_agent_by_name(self._run_id, recipient)
        if not agent:
            return

        label = "작업 위임" if msg_type == MessageType.DISPATCH else "결과 반환"
        summary = f"[{sender}] → [{recipient}] {label}"

        # AI CLI 터미널에 주입 프롬프트 전달
        if agent.surface_id:
            injection = self._prompt.build_injection_prompt(
                sender=sender,
                recipient=recipient,
                msg_type=msg_type,
                payload=payload,
            )
            if msg_type == MessageType.DISPATCH:
                injection = (
                    self._branch_safety_warning()
                    + self._workflow_state_hint()
                    + injection
                )
            # 1) recipient AI CLI가 작업 중이면 idle 될 때까지 대기.
            #    busy 상태에서 send_text하면 cmux input queue에 흡수되지 못하고 lost됨.
            idle_ok = self._wait_for_idle(agent.surface_id, max_wait=180.0)
            if not idle_ok:
                logger.warning(
                    "recipient=%s did not become idle within 180s; dispatching anyway",
                    recipient,
                )

            # 2) send + enter + 검증 + retry
            ok = self._send_with_verification(agent.surface_id, injection, max_retries=3)
            if not ok:
                logger.error(
                    "Dispatch to surface=%s remained stuck after 3 retries; "
                    "consider manual intervention",
                    agent.surface_id,
                )
            self._cmux.trigger_flash(surface_id=agent.surface_id)

        self._cmux.notify(title="cmux-agent", body=summary)
        self._cmux.log(summary, level="info", source="cmux-agent")

        if msg_type == MessageType.RESULT:
            self._maybe_auto_apply_result(payload)
