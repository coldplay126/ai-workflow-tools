"""역할별 prompt / delivery 메시지 생성."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cmux_agent.domain.models import AgentRole, MessageType

if TYPE_CHECKING:
    from cmux_agent.domain.models import Agent


ARTIFACT_FORMAT_DISPATCH = {
    "type": "dispatch",
    "sender": "orchestrator",
    "recipient": "<worker-name>",
    "message": "<구체적 작업 지시>",
}

ARTIFACT_FORMAT_RESULT = {
    "type": "result",
    "sender": "<worker-name>",
    "recipient": "orchestrator",
    "message": "<작업 결과 요약>",
}

ARTIFACT_FORMAT_CONTROL_SPAWN = {
    "type": "control",
    "sender": "orchestrator",
    "recipient": "controller",
    "message": "<왜 worker가 필요한지>",
    "action": "spawn_agent",
    "agent": {
        "name": "<worker-name 또는 생략>",
        "provider": "claude|codex|gemini",
        "flags": "<선택>",
    },
}


class PromptBuilder:
    """delivery 메시지, 주입 프롬프트, 프로토콜 파일을 생성한다."""

    def __init__(self, outbox_path: str, inbox_base: str) -> None:
        self._outbox = outbox_path
        self._inbox_base = inbox_base

    # -- Inbox delivery (JSON 파일) ----------------------------------------

    def build_delivery(
        self,
        *,
        sender: str,
        recipient: str,
        msg_type: MessageType,
        payload: dict,
    ) -> dict:
        now = datetime.now(UTC).isoformat()

        if msg_type == MessageType.DISPATCH:
            return self._dispatch_delivery(sender, recipient, payload, now)
        return self._result_delivery(sender, recipient, payload, now)

    def _dispatch_delivery(
        self, sender: str, recipient: str, payload: dict, ts: str,
    ) -> dict:
        return {
            "message_id": None,
            "from": sender,
            "type": "dispatch",
            "task": payload.get("message", ""),
            "context": payload.get("context", {}),
            "instructions": (
                f"작업 완료 후 {self._outbox} 에 result artifact(JSON)를 생성하세요."
            ),
            "artifact_format": {
                "type": "result",
                "sender": recipient,
                "recipient": sender,
                "message": "<작업 결과 요약>",
            },
            "created_at": ts,
        }

    def _result_delivery(
        self, sender: str, recipient: str, payload: dict, ts: str,
    ) -> dict:
        return {
            "message_id": None,
            "from": sender,
            "type": "result",
            "result": payload.get("message", ""),
            "context": payload.get("context", {}),
            "instructions": (
                f"추가 작업이 필요하면 {self._outbox} 에 dispatch artifact를 생성하세요."
            ),
            "artifact_format": {
                "type": "dispatch",
                "sender": recipient,
                "recipient": "<worker-name>",
                "message": "<작업 지시>",
            },
            "created_at": ts,
        }

    # -- 터미널 주입 프롬프트 (send_text용 자연어) ---------------------------

    def build_injection_prompt(
        self,
        *,
        sender: str,
        recipient: str,
        msg_type: MessageType,
        payload: dict,
    ) -> str:
        message = payload.get("message", "")

        if msg_type == MessageType.DISPATCH:
            # 프로토콜 파일이 있으면 참조 지시 추가
            protocol_path = Path(self._outbox).parent / f"{recipient.upper()}.md"
            protocol_hint = ""
            if protocol_path.exists():
                protocol_hint = (
                    f"\n**중요**: 작업 전 {protocol_path} 파일을 읽고 "
                    f"에이전트 정의와 I/O 계약을 따르세요.\n"
                )
            return (
                f"[cmux-agent] {sender}로부터 작업이 도착했습니다.\n"
                f"{protocol_hint}\n"
                f"작업: {message}\n"
                f"\n"
                f"위 작업을 수행하세요.\n"
                f"완료 후 {self._outbox} 에 아래 형식의 JSON 파일을 생성하세요.\n"
                f'{{"type": "result", "sender": "{recipient}", '
                f'"recipient": "{sender}", "message": "<작업 결과 요약>"}}'
            )

        return (
            f"[cmux-agent] {sender}의 작업 결과입니다.\n"
            f"\n"
            f"결과: {message}\n"
            f"\n"
            f"추가 작업이 필요하면 {self._outbox} 에 dispatch artifact를 생성하세요.\n"
            f"모든 작업이 완료되었으면 최종 결과를 보고하세요."
        )

    def build_startup_prompt(self, agent: Agent) -> str:
        """Build the first prompt injected after an AI CLI session starts."""
        base = Path(self._outbox).parent
        if agent.role == AgentRole.ORCHESTRATOR:
            protocol = base / "ORCHESTRATOR.md"
            common = base / "ORCHESTRATOR-COMMON.md"
            return (
                "[cmux-agent] startup protocol\n"
                f"작업 전 `{common}`와 `{protocol}`를 읽고 역할/I/O 계약을 따르세요.\n"
                f"worker 위임은 `{self._outbox}`에 dispatch artifact(JSON)로 작성하세요.\n"
                "작업 규모상 worker가 더 필요하면 control spawn_agent artifact를 작성하세요."
            )

        protocol = base / f"{agent.name.upper()}.md"
        common = base / "WORKER-COMMON.md"
        return (
            "[cmux-agent] startup protocol\n"
            f"작업 전 `{common}`와 `{protocol}`를 읽고 역할/I/O 계약을 따르세요.\n"
            "docs/templates/gap.md, docs/templates/status.md, docs/templates/test.md를 "
            "생성하거나 갱신할 때는 해당 템플릿의 closing/status/test 규칙을 따르세요.\n"
            f"할당된 작업 완료 후 `{self._outbox}`에 result artifact(JSON)를 작성하세요."
        )

    # -- 초기 프롬프트 -------------------------------------------------------

    def build_initial_orchestrator(self, workers: list[Agent]) -> dict:
        worker_list = [
            {"name": w.name, "role": w.role.value}
            for w in workers
            if w.role == AgentRole.WORKER
        ]
        return {
            "role": "orchestrator",
            "instructions": (
                "당신은 orchestrator입니다.\n"
                "분석, 계획, 작업 분해만 수행하세요.\n"
                "직접 파일을 수정하거나 명령을 실행하지 마세요.\n"
                f"worker에게 작업을 위임하려면 {self._outbox} 에 "
                "dispatch artifact(JSON)를 생성하세요.\n"
                "작업 크기나 분절 가능성상 worker가 더 필요하면 "
                "control spawn_agent artifact(JSON)를 생성하세요."
            ),
            "workers": worker_list,
            "outbox_path": self._outbox,
            "inbox_path": f"{self._inbox_base}/orchestrator",
            "artifact_format": ARTIFACT_FORMAT_DISPATCH,
            "control_format": ARTIFACT_FORMAT_CONTROL_SPAWN,
        }

    def build_initial_worker(self, name: str) -> dict:
        return {
            "role": "worker",
            "name": name,
            "instructions": (
                f"당신은 {name} worker입니다.\n"
                "할당된 작업을 수행하세요.\n"
                f"inbox({self._inbox_base}/{name})에서 작업을 확인하세요.\n"
                f"작업 완료 후 {self._outbox} 에 result artifact(JSON)를 생성하세요."
            ),
            "inbox_path": f"{self._inbox_base}/{name}",
            "outbox_path": self._outbox,
            "artifact_format": ARTIFACT_FORMAT_RESULT,
        }

    # -- 프로토콜 파일 생성 --------------------------------------------------

    def write_protocol_files(
        self,
        base_dir: str | Path,
        workers: list[Agent],
        template_dir: Path | None = None,
    ) -> None:
        """AI CLI가 읽을 프로토콜 파일을 .agent/ 에 생성한다.

        탐색 순서:
        1. template_dir/.agent-custom/ORCHESTRATOR.md (오케스트레이터)
        2. template_dir/../workers/ (공유 워커 프로토콜)
        3. 기본 생성 (위에서 못 찾은 경우)
        """
        base = Path(base_dir)
        custom_dir = None
        workers_dir = None
        if template_dir:
            candidate = template_dir / ".agent-custom"
            if candidate.is_dir():
                custom_dir = candidate
            # 공유 워커 디렉토리 (templates/cmux/workers/)
            candidate_workers = template_dir.parent / "workers"
            if candidate_workers.is_dir():
                workers_dir = candidate_workers

        # 공통 오케스트레이터 규칙 (templates/cmux/ORCHESTRATOR-COMMON.md)
        custom_names: set[str] = set()
        if template_dir:
            common_orch = template_dir.parent / "ORCHESTRATOR-COMMON.md"
            if common_orch.exists():
                (base / "ORCHESTRATOR-COMMON.md").write_text(
                    common_orch.read_text(encoding="utf-8"), encoding="utf-8"
                )
                custom_names.add("ORCHESTRATOR-COMMON")

        # 템플릿별 오케스트레이터 프로토콜 (.agent-custom/ 에서)
        if custom_dir and custom_dir.is_dir():
            for custom_file in custom_dir.glob("*.md"):
                target = base / custom_file.name
                target.write_text(custom_file.read_text(encoding="utf-8"), encoding="utf-8")
                custom_names.add(custom_file.stem)

        # 공유 워커 프로토콜 (workers/ 에서)
        if workers_dir:
            # WORKER-COMMON.md는 항상 복사
            common_file = workers_dir / "WORKER-COMMON.md"
            if common_file.exists() and "WORKER-COMMON" not in custom_names:
                (base / "WORKER-COMMON.md").write_text(
                    common_file.read_text(encoding="utf-8"), encoding="utf-8"
                )
                custom_names.add("WORKER-COMMON")
            # 각 워커에 매칭되는 프로토콜 파일 복사
            for w in workers:
                if w.role != AgentRole.WORKER:
                    continue
                upper_name = w.name.upper()
                worker_file = workers_dir / f"{upper_name}.md"
                if worker_file.exists() and upper_name not in custom_names:
                    (base / f"{upper_name}.md").write_text(
                        worker_file.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                    custom_names.add(upper_name)

        worker_names = [w.name for w in workers if w.role == AgentRole.WORKER]
        worker_list_str = "\n".join(f"- {n}" for n in worker_names)

        if "ORCHESTRATOR" not in custom_names:
            orch_content = (
                "# cmux-agent orchestrator 프로토콜\n"
                "\n"
                "당신은 orchestrator입니다.\n"
                "\n"
                "## 역할\n"
                "- 사용자의 요청을 분석하고 작업을 분해한다.\n"
                "- worker에게 작업을 위임한다.\n"
                "- 직접 파일을 수정하거나 명령을 실행하지 않는다.\n"
                "\n"
                "## 작업 위임 방법\n"
                f"{self._outbox} 디렉토리에 아래 형식의 JSON 파일을 생성한다.\n"
                "\n"
                "```json\n"
                + json.dumps(ARTIFACT_FORMAT_DISPATCH, ensure_ascii=False, indent=2)
                + "\n```\n"
                "\n"
                "## 동적 worker 생성\n"
                "- 작업 규모를 분석해 병렬화가 유효하면 새 worker를 요청한다.\n"
                "- provider는 작업 성격에 맞게 `claude`, `codex`, `gemini` 중 선택한다.\n"
                "- 새 worker가 생성되면 controller가 결과 메시지로 worker 이름을 알려준다.\n"
                "\n"
                "```json\n"
                + json.dumps(ARTIFACT_FORMAT_CONTROL_SPAWN, ensure_ascii=False, indent=2)
                + "\n```\n"
                "\n"
                "## 사용 가능한 worker\n"
                f"{worker_list_str}\n"
                "\n"
                "## 결과 수신\n"
                "worker의 결과는 이 터미널에 자동으로 전달된다.\n"
                "추가 작업이 필요하면 새로운 dispatch를 생성한다.\n"
                "모든 작업이 완료되면 사용자에게 최종 결과를 보고한다.\n"
            )
            (base / "ORCHESTRATOR.md").write_text(orch_content, encoding="utf-8")

        for name in worker_names:
            upper_name = name.upper()
            if upper_name in custom_names:
                continue
            fmt = {**ARTIFACT_FORMAT_RESULT, "sender": name}
            worker_content = (
                f"# cmux-agent {name} 프로토콜\n"
                f"\n"
                f"당신은 {name} worker입니다.\n"
                f"\n"
                f"## 역할\n"
                f"- orchestrator가 위임한 작업을 수행한다.\n"
                f"- 작업 완료 후 결과를 보고한다.\n"
                f"\n"
                f"## 작업 수신\n"
                f"이 터미널에 작업 지시가 자동으로 전달된다.\n"
                f"\n"
                f"## 결과 보고 방법\n"
                f"{self._outbox} 디렉토리에 아래 형식의 JSON 파일을 생성한다.\n"
                f"\n"
                f"```json\n"
                + json.dumps(fmt, ensure_ascii=False, indent=2)
                + "\n```\n"
                f"\n"
                f"## 팀 작업\n"
                f"- orchestrator가 명시적으로 허용한 경우 다른 worker에게 dispatch를 보낼 수 있다.\n"
                f"- 이때 sender는 반드시 `{name}`으로 기록하고 recipient는 대상 worker 이름으로 기록한다.\n"
            )
            (base / f"{name.upper()}.md").write_text(worker_content, encoding="utf-8")
