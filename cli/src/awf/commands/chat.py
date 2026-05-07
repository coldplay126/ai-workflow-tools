from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from awf.core.chat_session import (
    append_message,
    compact_session,
    create_session,
    ensure_session_db,
    get_session,
    latest_session_id,
    list_sessions,
    load_messages,
    update_latest_message_usage,
)
from awf.core.config import load_awf_config, resolve_runtime_paths
from awf.core.permissions import PermissionDeniedError, build_permission_ruleset, check_permission, provider_permission_name
from awf.core.readiness import maybe_doctor_hint
from awf.providers.registry import ProviderRegistry, UnknownProviderError


AUTO_COMPACT_MAX_MESSAGES = 6
COMPACTION_KEEP_LAST = 4


def _apply_provider_permission_mode(provider, *, yolo: bool) -> None:
    if yolo and hasattr(provider, "set_permission_mode"):
        provider.set_permission_mode("bypassPermissions")


def _build_chat_prompt(messages: list[dict[str, str]]) -> str:
    from awf.core.spec_loader import load_prompt_optional
    header = load_prompt_optional("chat", "session-header")
    if header:
        lines = [header, ""]
    else:
        lines = ["You are running inside awf chat session mode.", ""]
    for message in messages:
        lines.append(f"{message['role'].upper()}: {message['content']}")
    lines.append("")
    lines.append("ASSISTANT:")
    return "\n".join(lines)


def _session_db_path(config, repo_root: str | None) -> Path:
    env_override = os.environ.get("AWF_SESSION_DB", "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()
    override = config.path_override("session_db")
    if override:
        return Path(override).expanduser().resolve()
    resolve_runtime_paths(repo_root)
    return (Path.home() / ".local" / "share" / "awf" / "awf.db").resolve()


def _resolve_or_create_session(db_path: Path, provider_name: str, session_id: str | None) -> tuple[str, bool]:
    if session_id:
        existing = get_session(db_path, session_id)
        if existing is None:
            raise KeyError(f"Unknown chat session: {session_id}")
        return existing.id, False
    created = create_session(db_path, provider_name)
    return created.id, True


def _resolve_requested_session_id(db_path: Path, session_id: str | None, latest: bool) -> str | None:
    if session_id:
        return session_id
    if latest:
        return latest_session_id(db_path)
    return None


def _print_compaction_result(session_id: str, result: dict[str, int | bool], *, as_json: bool) -> None:
    payload = {"session_id": session_id, **result}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"session_id: {session_id}")
    print(f"compacted: {result['compacted']}")
    print(f"original_count: {result['original_count']}")
    print(f"new_count: {result['new_count']}")
    print(f"summarized_count: {result['summarized_count']}")
    print(f"summary_mode: {result['summary_mode']}")


def _print_session_messages(session_id: str, messages, *, as_json: bool) -> None:
    if as_json:
        payload = {
            "session_id": session_id,
            "messages": [
                {
                    "role": item.role,
                    "content": item.content,
                    "created_at": item.created_at,
                    "token_estimate": item.token_estimate,
                    "cost_usd_estimate": item.cost_usd_estimate,
                }
                for item in messages
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"session_id: {session_id}")
    for item in messages:
        print(
            f"[{item.created_at}] {item.role} "
            f"(tokens~{item.token_estimate}, cost~${item.cost_usd_estimate:.6f}): {item.content}"
        )


def _estimate_tokens(text: str) -> int:
    compact = text.strip()
    if not compact:
        return 0
    return max(1, len(compact) // 4)


def _provider_cost_rates(config, provider_name: str) -> tuple[float, float]:
    provider_settings = config.raw.get("provider", {}).get(provider_name, {})
    input_rate = float(provider_settings.get("input_cost_per_1m_tokens", 0) or 0)
    output_rate = float(provider_settings.get("output_cost_per_1m_tokens", 0) or 0)
    return input_rate, output_rate


def _estimate_cost_usd(token_estimate: int, rate_per_1m_tokens: float) -> float:
    if token_estimate <= 0 or rate_per_1m_tokens <= 0:
        return 0.0
    return (float(token_estimate) / 1_000_000.0) * rate_per_1m_tokens


def _compaction_summary_source(messages, *, max_messages: int = AUTO_COMPACT_MAX_MESSAGES, keep_last: int = COMPACTION_KEEP_LAST):
    if len(messages) < max_messages:
        return []
    keep_last = max(1, keep_last)
    return list(messages[:-keep_last])


def _build_compaction_prompt(messages) -> str:
    lines = [
        "Summarize the earlier chat turns for compaction.",
        "Return plain text only.",
        "Keep it concise and practical.",
        "Capture user intent, important answers, decisions, and unresolved follow-ups.",
        "",
    ]
    for item in messages:
        lines.append(f"{item.role.upper()}: {item.content}")
    return "\n".join(lines)


def _summarize_compaction(provider, messages, cwd: str) -> str | None:
    summary_source = _compaction_summary_source(messages)
    if not summary_source:
        return None
    result = provider.complete(_build_compaction_prompt(summary_source), cwd=cwd, add_dirs=None)
    if result.returncode != 0:
        return None
    content = (result.stdout or result.stderr or "").strip()
    if not content:
        return None
    if not content.startswith("Session summary of earlier turns:"):
        content = "Session summary of earlier turns:\n" + content
    return content


def _compact_session_with_provider(db_path: Path, session_id: str, provider, cwd: str) -> dict[str, int | bool]:
    messages = load_messages(db_path, session_id)
    summary_content = _summarize_compaction(provider, messages, cwd)
    return compact_session(
        db_path,
        session_id,
        max_messages=AUTO_COMPACT_MAX_MESSAGES,
        keep_last=COMPACTION_KEEP_LAST,
        summary_content=summary_content,
    )


def _run_turn(provider, config, provider_name: str, cwd: str, session_id: str, db_path: Path, user_text: str) -> dict[str, object]:
    existing_messages = load_messages(db_path, session_id)
    if len(existing_messages) >= AUTO_COMPACT_MAX_MESSAGES:
        _compact_session_with_provider(db_path, session_id, provider, cwd)
    input_rate, output_rate = _provider_cost_rates(config, provider_name)
    input_tokens = _estimate_tokens(user_text)
    append_message(
        db_path,
        session_id,
        "user",
        user_text,
        token_estimate=input_tokens,
        cost_usd_estimate=_estimate_cost_usd(input_tokens, input_rate),
    )
    history = load_messages(db_path, session_id)
    prompt = _build_chat_prompt([{"role": item.role, "content": item.content} for item in history])
    result = provider.complete(prompt, cwd=cwd, add_dirs=None)
    assistant_text = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(assistant_text or "provider chat turn failed")
    native_usage = getattr(result, "usage", None)
    native_input_tokens = int(getattr(native_usage, "input_tokens", 0) or 0)
    native_output_tokens = int(getattr(native_usage, "output_tokens", 0) or 0)
    usage_source = "provider" if native_input_tokens > 0 or native_output_tokens > 0 else "estimated"
    input_token_value = native_input_tokens or input_tokens
    if native_input_tokens > 0 and native_input_tokens != input_tokens:
        update_latest_message_usage(
            db_path,
            session_id,
            "user",
            token_estimate=native_input_tokens,
            cost_usd_estimate=_estimate_cost_usd(native_input_tokens, input_rate),
        )
    output_tokens = native_output_tokens or _estimate_tokens(assistant_text)
    append_message(
        db_path,
        session_id,
        "assistant",
        assistant_text,
        token_estimate=output_tokens,
        cost_usd_estimate=_estimate_cost_usd(output_tokens, output_rate),
    )
    session = get_session(db_path, session_id)
    usage = {
        "usage_source": usage_source,
        "input_tokens_estimate": input_token_value,
        "output_tokens_estimate": output_tokens,
        "turn_cost_usd_estimate": _estimate_cost_usd(input_token_value, input_rate) + _estimate_cost_usd(output_tokens, output_rate),
        "session_token_input_estimate": session.token_input_estimate if session else input_token_value,
        "session_token_output_estimate": session.token_output_estimate if session else output_tokens,
        "session_cost_usd_estimate": session.cost_usd_estimate if session else 0.0,
    }
    return {"response": assistant_text, "usage": usage}


def run_chat(args: argparse.Namespace) -> int:
    try:
        config = load_awf_config(args.repo_root)
        provider_name = args.provider or config.provider_name()
        db_path = ensure_session_db(_session_db_path(config, args.repo_root))
    except PermissionDeniedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "list_sessions", False):
        sessions = list_sessions(db_path)
        if args.json:
            print(json.dumps({"sessions": sessions}, ensure_ascii=False, indent=2))
        else:
            print(f"session_db: {db_path}")
            for item in sessions:
                print(
                    f"- {item['id']} provider={item['provider']} messages={item['message_count']} "
                    f"tokens_in~{item['token_input_estimate']} tokens_out~{item['token_output_estimate']} "
                    f"cost~${float(item['cost_usd_estimate']):.6f} updated_at={item['updated_at']} title={item['title']}"
                )
        return 0

    if getattr(args, "show_latest", False):
        target_session_id = latest_session_id(db_path)
        if not target_session_id:
            print("error: No chat session available", file=sys.stderr)
            return 2
        messages = load_messages(db_path, target_session_id)
        _print_session_messages(target_session_id, messages, as_json=bool(args.json))
        return 0

    if getattr(args, "show_session", None):
        messages = load_messages(db_path, args.show_session)
        _print_session_messages(args.show_session, messages, as_json=bool(args.json))
        return 0

    if getattr(args, "compact_session", None) or getattr(args, "compact_latest", False):
        try:
            target_session_id = _resolve_requested_session_id(
                db_path,
                getattr(args, "compact_session", None),
                bool(getattr(args, "compact_latest", False)),
            )
            if not target_session_id:
                raise KeyError("No chat session available for compaction")
            if get_session(db_path, target_session_id) is None:
                raise KeyError(f"Unknown chat session: {target_session_id}")
            session = get_session(db_path, target_session_id)
            provider_for_compaction = None
            if session is not None:
                try:
                    ruleset = build_permission_ruleset(config.raw, yolo=getattr(args, "yolo", False))
                    provider_name = args.provider or session.provider or config.provider_name()
                    check_permission(ruleset, provider_permission_name(provider_name, config.raw.get("provider", {}).get("aliases")), "chat")
                    provider_for_compaction = ProviderRegistry(config).get(provider_name)
                    _apply_provider_permission_mode(provider_for_compaction, yolo=bool(getattr(args, "yolo", False)))
                except Exception:
                    provider_for_compaction = None
            if provider_for_compaction is not None:
                result = _compact_session_with_provider(db_path, target_session_id, provider_for_compaction, str(Path.cwd()))
            else:
                result = compact_session(db_path, target_session_id)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        _print_compaction_result(target_session_id, result, as_json=bool(args.json))
        return 0

    try:
        ruleset = build_permission_ruleset(config.raw, yolo=getattr(args, "yolo", False))
        check_permission(ruleset, provider_permission_name(provider_name, config.raw.get("provider", {}).get("aliases")), "chat")
        registry = ProviderRegistry(config)
        provider = registry.get(provider_name)
        _apply_provider_permission_mode(provider, yolo=bool(getattr(args, "yolo", False)))
        requested_session_id = _resolve_requested_session_id(db_path, args.session_id, bool(getattr(args, "latest", False)))
        session_id, created = _resolve_or_create_session(db_path, provider_name, requested_session_id)
    except PermissionDeniedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except UnknownProviderError:
        print(f"error: unsupported provider `{args.provider}`", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if created:
        print(f"session_id: {session_id}")
        print(f"session_db: {db_path}")

    repo_root = str(Path(args.repo_root).resolve()) if args.repo_root else str(Path.cwd())

    if args.message:
        try:
            turn = _run_turn(provider, config, provider_name, repo_root, session_id, db_path, args.message)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            hint = maybe_doctor_hint(provider_name, str(exc))
            if hint:
                print(hint, file=sys.stderr)
            return 1
        if args.json:
            payload = {
                "session_id": session_id,
                "provider": provider_name,
                "response": turn["response"],
                "usage": turn["usage"],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(turn["response"])
            usage = turn["usage"]
            print(
                "usage_estimate: "
                f"in={usage['input_tokens_estimate']} "
                f"out={usage['output_tokens_estimate']} "
                f"session_in={usage['session_token_input_estimate']} "
                f"session_out={usage['session_token_output_estimate']} "
                f"turn_cost=${float(usage['turn_cost_usd_estimate']):.6f} "
                f"session_cost=${float(usage['session_cost_usd_estimate']):.6f}"
            )
        return 0

    print("chat_ready: type `/exit` to quit")
    while True:
        try:
            user_text = input("awf> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return 130
        if not user_text:
            continue
        if user_text in {"/exit", "/quit"}:
            return 0
        try:
            turn = _run_turn(provider, config, provider_name, repo_root, session_id, db_path, user_text)
        except KeyboardInterrupt:
            print(file=sys.stderr)
            return 130
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            hint = maybe_doctor_hint(provider_name, str(exc))
            if hint:
                print(hint, file=sys.stderr)
            continue
        print(turn["response"])
        usage = turn["usage"]
        print(
            "usage_estimate: "
            f"in={usage['input_tokens_estimate']} "
            f"out={usage['output_tokens_estimate']} "
            f"session_in={usage['session_token_input_estimate']} "
            f"session_out={usage['session_token_output_estimate']} "
            f"turn_cost=${float(usage['turn_cost_usd_estimate']):.6f} "
            f"session_cost=${float(usage['session_cost_usd_estimate']):.6f}"
        )
