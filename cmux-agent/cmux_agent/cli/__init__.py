"""CLI 진입점."""

from __future__ import annotations

import argparse
import sys

from cmux_agent.cli.commands import (
    cmd_agents,
    cmd_doctor,
    cmd_events,
    cmd_failures,
    cmd_messages,
    cmd_recover,
    cmd_register,
    cmd_send,
    cmd_smoke,
    cmd_spawn,
    cmd_start,
    cmd_status,
    cmd_stop,
    cmd_task,
    cmd_watch,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmux-agent",
        description="cmux 기반 멀티 에이전트 메시지 브로커",
    )
    # 최상위 옵션 (start 없이도 사용 가능)
    parser.add_argument("--cwd", default=".", help="작업 디렉토리")
    parser.add_argument("--template", help="cmux 템플릿 (feature, bugfix, review 또는 경로)")
    parser.add_argument("--templates-dir", help="템플릿 루트 디렉토리 (기본: CMUX_TEMPLATES_DIR 환경변수)")

    sub = parser.add_subparsers(dest="command")

    # doctor
    sub.add_parser("doctor", help="시스템 진단")

    # smoke
    p_smoke = sub.add_parser("smoke", help="실제 cmux runtime smoke test")
    p_smoke.add_argument("--cwd", dest="smoke_cwd", help="smoke 작업 디렉토리 (기본: 임시 디렉토리)")
    p_smoke.add_argument("--template", dest="smoke_template", default="review", help="시작 템플릿 (기본: review)")
    p_smoke.add_argument("--templates-dir", dest="smoke_templates_dir", help="템플릿 루트 디렉토리")
    p_smoke.add_argument("--worker-template", default="test", help="동적으로 생성할 worker template (기본: test)")
    p_smoke.add_argument("--provider", default="codex", help="동적 worker provider (기본: codex)")
    p_smoke.add_argument("--timeout", type=float, default=20.0, help="spawn/result 대기 초")
    p_smoke.add_argument("--poll-interval", type=float, default=0.5, help="poll 간격 초")
    p_smoke.add_argument("--keep", action="store_true", help="smoke workspace와 .agent 파일을 보존")

    # start
    p_start = sub.add_parser("start", help="새 run 시작")
    p_start.add_argument(
        "--attach-orchestrator",
        action="store_true",
        help="현재 CLI/AI 세션을 orchestrator로 등록하고 worker/controller만 cmux에 생성",
    )

    # task
    p_task = sub.add_parser("task", help="orchestrator에 작업 주입")
    p_task.add_argument("request", help="작업 요청 내용")

    # stop
    p_stop = sub.add_parser("stop", help="run 종료")
    p_stop.add_argument("run_id", nargs="?", help="run ID (기본: 최근)")
    p_stop.add_argument("--no-clean", dest="clean", action="store_false", help=".agent/ 디렉토리 유지")
    p_stop.add_argument(
        "--keep-workspace",
        action="store_true",
        help="cmux workspace/surface를 닫지 않고 유지 (디버깅용). "
             "기본 동작: §2.9에 따라 run의 surface와 workspace를 자동으로 닫는다.",
    )

    # recover
    p_recover = sub.add_parser(
        "recover",
        help="stale workspace 감지 시 run을 FAILED로 마킹하고 watcher lock을 정리 (§2.10)",
    )
    p_recover.add_argument(
        "--force",
        action="store_true",
        help="workspace가 살아있어도 강제로 cleanup",
    )

    # register
    p_reg = sub.add_parser("register", help="agent 등록")
    p_reg.add_argument("name", help="agent 이름")
    p_reg.add_argument(
        "--role",
        choices=["orchestrator", "worker"],
        default="worker",
        help="역할 (기본: worker)",
    )
    p_reg.add_argument("--surface-id", help="cmux surface ID")

    # spawn
    p_spawn = sub.add_parser("spawn", help="새 worker 세션 생성")
    p_spawn.add_argument("name", nargs="?", help="worker 이름 (목적이 없으면 기본: 다음 worker-auto-N)")
    p_spawn.add_argument("--role", help="worker 목적 역할 (예: review, test, fix)")
    p_spawn.add_argument("--worker-template", help="worker 프로토콜 템플릿 이름 (예: review, test)")
    p_spawn.add_argument("--provider", help="provider 이름 (기본: 설정 또는 claude)")
    p_spawn.add_argument("--flags", default="", help="provider 실행 플래그")

    # agents
    p_agents = sub.add_parser("agents", help="등록된 agent 목록")
    p_agents.add_argument("run_id", nargs="?", help="run ID (생략 시 활성 run만 조회)")
    p_agents.add_argument(
        "--json",
        action="store_true",
        help="JSON 출력 (활성 run이 없으면 run_id=null, agents=[]).",
    )

    # watch
    p_watch = sub.add_parser("watch", help="outbox watcher 시작")
    p_watch.add_argument("--daemon", action="store_true", help="백그라운드 모드")

    # status
    p_status = sub.add_parser("status", help="run 상태 조회")
    p_status.add_argument("run_id", nargs="?", help="run ID")
    p_status.add_argument("--failures", action="store_true", help="최근 실패 상세 출력")
    p_status.add_argument("-n", "--failure-limit", type=int, default=10, help="최근 실패 N건")

    # events
    p_events = sub.add_parser("events", help="이벤트 로그 조회")
    p_events.add_argument("run_id", nargs="?", help="run ID")
    p_events.add_argument("-n", "--limit", type=int, default=20, help="최근 N건")
    p_events.add_argument("--failures", action="store_true", help="실패 이벤트만 출력")

    # failures
    p_failures = sub.add_parser("failures", help="최근 실패 artifact와 이유 조회")
    p_failures.add_argument("run_id", nargs="?", help="run ID")
    p_failures.add_argument("-n", "--limit", type=int, default=10, help="최근 실패 N건")

    # send
    p_send = sub.add_parser("send", help="수동 메시지 전송")
    p_send.add_argument("recipient", help="수신자 agent 이름")
    p_send.add_argument("message", help="메시지 내용")

    # messages
    p_msg = sub.add_parser("messages", help="메시지 이력 조회")
    p_msg.add_argument("run_id", nargs="?", help="run ID")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        args.command = "start"

    # --cwd를 전역으로 설정하여 모든 명령어가 참조
    from cmux_agent.cli import commands as _cmd_mod
    _cmd_mod._active_cwd = getattr(args, "cwd", ".") or "."

    commands = {
        "doctor": cmd_doctor,
        "smoke": cmd_smoke,
        "start": cmd_start,
        "task": cmd_task,
        "stop": cmd_stop,
        "recover": cmd_recover,
        "register": cmd_register,
        "spawn": cmd_spawn,
        "agents": cmd_agents,
        "watch": cmd_watch,
        "status": cmd_status,
        "events": cmd_events,
        "failures": cmd_failures,
        "send": cmd_send,
        "messages": cmd_messages,
    }

    handler = commands.get(args.command)
    if handler:
        try:
            handler(args)
        except Exception as exc:  # noqa: BLE001
            print(f"오류: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)
