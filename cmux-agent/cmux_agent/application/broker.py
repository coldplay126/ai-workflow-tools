"""메시지 브로커 — artifact를 파싱하고 수신자 inbox에 전달한다."""

from __future__ import annotations

import json
import logging
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
