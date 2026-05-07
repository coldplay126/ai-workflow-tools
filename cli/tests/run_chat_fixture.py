from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _write_project_config(path: Path, db_path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "[provider]",
                'default = "fixture"',
                "",
                "[provider.fixture]",
                'result_file = "cli/tests/fixtures/review-result.json"',
                "",
                "[paths]",
                f'session_db = "{db_path}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _run_chat(message: str, session_id: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_SESSION_DB"] = str(_run_chat.db_path)
    env["AWF_FIXTURE_RESULT_FILE"] = str(ROOT / "cli" / "tests" / "fixtures" / "review-result.json")
    env["AWF_FIXTURE_USAGE_JSON"] = json.dumps({"input_tokens": 11, "output_tokens": 22})
    env["AWF_FIXTURE_CHAT_SUMMARY"] = "- user goals preserved\n- assistant conclusions preserved"
    cmd = [
        sys.executable,
        "-m",
        "awf",
        "chat",
        "--repo-root",
        str(ROOT),
        "--provider",
        "fixture",
        "--message",
        message,
        "--json",
        "--yolo",
    ]
    if session_id:
        cmd.extend(["--session-id", session_id])
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _run_chat_latest(message: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_SESSION_DB"] = str(_run_chat.db_path)
    env["AWF_FIXTURE_RESULT_FILE"] = str(ROOT / "cli" / "tests" / "fixtures" / "review-result.json")
    env["AWF_FIXTURE_USAGE_JSON"] = json.dumps({"input_tokens": 11, "output_tokens": 22})
    env["AWF_FIXTURE_CHAT_SUMMARY"] = "- user goals preserved\n- assistant conclusions preserved"
    cmd = [
        sys.executable,
        "-m",
        "awf",
        "chat",
        "--repo-root",
        str(ROOT),
        "--provider",
        "fixture",
        "--latest",
        "--message",
        message,
        "--json",
        "--yolo",
    ]
    return subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)


def _run_chat_list() -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_SESSION_DB"] = str(_run_chat.db_path)
    env["AWF_FIXTURE_USAGE_JSON"] = json.dumps({"input_tokens": 11, "output_tokens": 22})
    env["AWF_FIXTURE_CHAT_SUMMARY"] = "- user goals preserved\n- assistant conclusions preserved"
    cmd = [
        sys.executable,
        "-m",
        "awf",
        "chat",
        "--repo-root",
        str(ROOT),
        "--list-sessions",
        "--json",
    ]
    return subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)


def _run_chat_show(session_id: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_SESSION_DB"] = str(_run_chat.db_path)
    env["AWF_FIXTURE_USAGE_JSON"] = json.dumps({"input_tokens": 11, "output_tokens": 22})
    env["AWF_FIXTURE_CHAT_SUMMARY"] = "- user goals preserved\n- assistant conclusions preserved"
    cmd = [
        sys.executable,
        "-m",
        "awf",
        "chat",
        "--repo-root",
        str(ROOT),
        "--show-session",
        session_id,
        "--json",
    ]
    return subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)


def _run_chat_compact_latest() -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "cli" / "src")
    env["AWF_SESSION_DB"] = str(_run_chat.db_path)
    env["AWF_FIXTURE_USAGE_JSON"] = json.dumps({"input_tokens": 11, "output_tokens": 22})
    env["AWF_FIXTURE_CHAT_SUMMARY"] = "- user goals preserved\n- assistant conclusions preserved"
    cmd = [
        sys.executable,
        "-m",
        "awf",
        "chat",
        "--repo-root",
        str(ROOT),
        "--compact-latest",
        "--json",
    ]
    return subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)


def _run_chat_auto(message: str, session_id: str) -> subprocess.CompletedProcess[str]:
    return _run_chat(message, session_id=session_id)


def main() -> int:
    config_path = ROOT / ".awf.toml"
    backup = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    try:
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            db_path = tmp_dir / "awf.db"
            _run_chat.db_path = db_path
            _write_project_config(config_path, db_path)

            first = _run_chat("hello")
            print(first.stdout, end="")
            if first.stderr:
                print(first.stderr, file=sys.stderr, end="")
            if first.returncode != 0:
                return first.returncode

            first_json_start = first.stdout.find("{")
            if first_json_start == -1:
                return 1
            first_payload = json.loads(first.stdout[first_json_start:])
            session_id = str(first_payload["session_id"])
            print(f"chat_session_id={session_id}")
            if str(first_payload["usage"]["usage_source"]) != "provider":
                return 1
            if int(first_payload["usage"]["input_tokens_estimate"]) != 11:
                return 1
            if int(first_payload["usage"]["output_tokens_estimate"]) != 22:
                return 1

            second = _run_chat("again", session_id=session_id)
            print(second.stdout, end="")
            if second.stderr:
                print(second.stderr, file=sys.stderr, end="")
            if second.returncode != 0:
                return second.returncode

            latest = _run_chat_latest("from-latest")
            print(latest.stdout, end="")
            if latest.stderr:
                print(latest.stderr, file=sys.stderr, end="")
            if latest.returncode != 0:
                return latest.returncode
            latest_payload = json.loads(latest.stdout)
            if int(latest_payload["usage"]["session_token_input_estimate"]) != 33:
                return 1

            listed = _run_chat_list()
            print(listed.stdout, end="")
            if listed.stderr:
                print(listed.stderr, file=sys.stderr, end="")
            if listed.returncode != 0:
                return listed.returncode
            listed_payload = json.loads(listed.stdout)

            shown = _run_chat_show(session_id)
            print(shown.stdout, end="")
            if shown.stderr:
                print(shown.stderr, file=sys.stderr, end="")
            if shown.returncode != 0:
                return shown.returncode
            shown_payload = json.loads(shown.stdout)

            auto = _run_chat_auto("after-auto", session_id)
            print(auto.stdout, end="")
            if auto.stderr:
                print(auto.stderr, file=sys.stderr, end="")
            if auto.returncode != 0:
                return auto.returncode

            auto_shown = _run_chat_show(session_id)
            print(auto_shown.stdout, end="")
            if auto_shown.stderr:
                print(auto_shown.stderr, file=sys.stderr, end="")
            if auto_shown.returncode != 0:
                return auto_shown.returncode
            auto_shown_payload = json.loads(auto_shown.stdout)
            auto_messages = auto_shown_payload.get("messages", [])
            if len(auto_messages) != 7:
                return 1
            if str(auto_messages[0].get("role")) != "system":
                return 1
            if "Session summary of earlier turns:" not in str(auto_messages[0].get("content")):
                return 1
            if "user goals preserved" not in str(auto_messages[0].get("content")):
                return 1

            compacted = _run_chat_compact_latest()
            print(compacted.stdout, end="")
            if compacted.stderr:
                print(compacted.stderr, file=sys.stderr, end="")
            if compacted.returncode != 0:
                return compacted.returncode
            compacted_payload = json.loads(compacted.stdout)

            compacted_shown = _run_chat_show(session_id)
            print(compacted_shown.stdout, end="")
            if compacted_shown.stderr:
                print(compacted_shown.stderr, file=sys.stderr, end="")
            if compacted_shown.returncode != 0:
                return compacted_shown.returncode
            compacted_shown_payload = json.loads(compacted_shown.stdout)

            with sqlite3.connect(str(db_path)) as conn:
                session_count = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
                message_count = conn.execute("SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)).fetchone()[0]
            print(f"chat_session_count={session_count}")
            print(f"chat_message_count={message_count}")
            if int(session_count) != 1:
                return 1
            if int(message_count) != 5:
                return 1
            if len(listed_payload.get("sessions", [])) != 1:
                return 1
            if str(listed_payload["sessions"][0]["id"]) != session_id:
                return 1
            if int(listed_payload["sessions"][0]["token_input_estimate"]) != 33:
                return 1
            if int(listed_payload["sessions"][0]["token_output_estimate"]) != 66:
                return 1
            if str(latest_payload["session_id"]) != session_id:
                return 1
            if len(shown_payload.get("messages", [])) != 6:
                return 1
            if int(shown_payload["messages"][0]["token_estimate"]) != 11:
                return 1

            if not bool(compacted_payload.get("compacted")):
                return 1
            if int(compacted_payload.get("new_count", 0)) != 5:
                return 1
            if str(compacted_payload.get("summary_mode")) != "provider":
                return 1
            compacted_messages = compacted_shown_payload.get("messages", [])
            if len(compacted_messages) != 5:
                return 1
            if int(compacted_messages[0]["token_estimate"]) <= 0:
                return 1
            if str(compacted_messages[0].get("role")) != "system":
                return 1
            if "Session summary of earlier turns:" not in str(compacted_messages[0].get("content")):
                return 1
            if "assistant conclusions preserved" not in str(compacted_messages[0].get("content")):
                return 1
            return 0
    finally:
        if backup is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_text(backup, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
