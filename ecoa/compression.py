import json
import re
import time

from .config import (
    COMPACT_IMPORTANCE_MIN_SCORE,
    COMPACT_INPUT_CHAR_BUDGET,
    COMPACT_KEEP_RECENT_MESSAGES,
    COMPACT_LLM_CANDIDATE_LIMIT,
    COMPACT_MAX_IMPORTANT_RECORDS,
    COMPACT_MAX_RECORD_CHARS,
    COMPACT_USE_WEAK_LLM_SCORING,
    KEEP_RECENT,
    TRANSCRIPT_DIR,
    get_model_profile,
)
from .todo import TODO

# %% ------------------------- compression -------------------------

COMPRESSION_MARKER = "[Conversation compressed."

REQUIREMENT_KEYWORDS = (
    "requirement",
    "requirements",
    "need",
    "needs",
    "must",
    "should",
    "constraint",
    "constraints",
    "decision",
    "decide",
    "decided",
    "confirm",
    "confirmed",
    "agree",
    "agreed",
    "do not",
    "don't",
    "avoid",
    "forbid",
    "preserve",
    "keep",
    "implement",
    "fix",
    "change",
    "modify",
    "update",
    "决定",
    "确认",
    "同意",
    "需求",
    "要求",
    "约束",
    "必须",
    "应该",
    "不要",
    "不能",
    "禁止",
    "避免",
    "保留",
    "修改为",
    "实现",
    "修复",
    "当前",
)

PROTOCOL_KEYWORDS = (
    "plan_approval",
    "approval",
    "approve",
    "approved",
    "reject",
    "rejected",
    "next_action",
    "shutdown_request",
    "shutdown_response",
    "request_id",
)

ERROR_KEYWORDS = (
    "Error",
    "Traceback",
    "Exception",
    "failed",
    "failure",
    "blocked",
    "blocker",
    "timeout",
    "异常",
    "报错",
    "错误",
    "失败",
    "阻塞",
)

STATE_KEYWORDS = (
    "TODO",
    "todo",
    "pending",
    "in_progress",
    "completed",
    "current state",
    "next step",
    "open item",
    "未完成",
    "已完成",
    "当前状态",
    "下一步",
)

FILE_REF_RE = re.compile(
    r"(?i)("
    r"[A-Za-z]:[\\/][^\s\"'<>]+"
    r"|(?:\.{1,2}[\\/])?[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+(?::\d+)?"
    r"|[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|json|md|txt|yml|yaml|toml|css|html)"
    r")"
)
SYMBOL_RE = re.compile(r"\b(?:def|class)\s+[A-Za-z_][A-Za-z0-9_]*|\b[A-Za-z_][A-Za-z0-9_]*\(")


def estimate_tokens(messages: list) -> int:
    return len(str(messages)) // 4


def _truncate_text(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... (truncated)"


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return _json_dumps(value)
    except Exception:
        return str(value)


def _block_type(block) -> str:
    if isinstance(block, dict):
        return str(block.get("type") or "")
    return str(getattr(block, "type", "") or "")


def _block_attr(block, name: str, default=None):
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _block_text(block) -> str:
    block_type = _block_type(block)
    if block_type == "text":
        return str(_block_attr(block, "text", "") or "")
    if block_type == "tool_result":
        return _stringify(_block_attr(block, "content", ""))
    if block_type == "tool_use":
        name = _block_attr(block, "name", "unknown")
        tool_input = _block_attr(block, "input", {})
        return f"tool_use {name}: {_stringify(tool_input)}"
    if hasattr(block, "text"):
        return str(getattr(block, "text") or "")
    return _stringify(block)


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    for keyword in keywords:
        if keyword.lower() in lowered or keyword in text:
            return True
    return False


def _strip_tag_block(text: str, tag: str) -> str:
    return re.sub(rf"\n*<{tag}>.*?</{tag}>\s*", "\n", text, flags=re.DOTALL).strip()


def extract_critical(messages):
    errors = []
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    content = _stringify(part.get("content", ""))
                    if _contains_any(content, ERROR_KEYWORDS):
                        errors.append(content[-2000:])
    return errors[-1] if errors else None


def extract_previous_summary(messages: list) -> str:
    for msg in reversed(messages):
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        if COMPRESSION_MARKER not in content[:300]:
            continue
        marker_end = content.find("]\n\n")
        if marker_end != -1:
            summary = content[marker_end + 3:].strip()
        else:
            summary = content.strip()
        summary = _strip_tag_block(summary, "todos")
        return _truncate_text(summary, COMPACT_INPUT_CHAR_BUDGET // 3)
    return ""


def _is_compressed_summary_message(msg: dict) -> bool:
    content = msg.get("content", "")
    return isinstance(content, str) and COMPRESSION_MARKER in content[:300]


def _build_tool_name_map(messages: list) -> dict:
    tool_name_map = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if _block_type(block) == "tool_use":
                tool_id = _block_attr(block, "id", "")
                tool_name = _block_attr(block, "name", "unknown")
                if tool_id:
                    tool_name_map[tool_id] = tool_name
    return tool_name_map


def _make_record(
    msg_idx: int,
    block_idx: int,
    role: str,
    kind: str,
    text: str,
    total_messages: int,
    tool_name: str | None = None,
    tool_use_id: str | None = None,
) -> dict:
    text = str(text or "").strip()
    return {
        "id": f"m{msg_idx}.b{block_idx}",
        "message_index": msg_idx,
        "block_index": block_idx,
        "role": role,
        "kind": kind,
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "text": text,
        "char_count": len(text),
        "is_recent": msg_idx >= max(0, total_messages - COMPACT_KEEP_RECENT_MESSAGES),
        "rule_score": 0,
        "llm_score": None,
        "final_score": 0,
        "reasons": [],
        "protected": False,
    }


def build_compaction_records(messages: list) -> list[dict]:
    records = []
    total = len(messages)
    tool_name_map = _build_tool_name_map(messages)
    for msg_idx, msg in enumerate(messages):
        if _is_compressed_summary_message(msg):
            continue
        role = str(msg.get("role", "unknown"))
        content = msg.get("content", "")
        if isinstance(content, str):
            kind = f"{role}_text" if role in ("user", "assistant") else "message_text"
            records.append(_make_record(msg_idx, 0, role, kind, content, total))
            continue
        if not isinstance(content, list):
            records.append(_make_record(msg_idx, 0, role, "message_text", _stringify(content), total))
            continue
        for block_idx, block in enumerate(content):
            block_type = _block_type(block)
            if block_type == "tool_result":
                tool_use_id = str(_block_attr(block, "tool_use_id", "") or "")
                records.append(_make_record(
                    msg_idx,
                    block_idx,
                    role,
                    "tool_result",
                    _block_text(block),
                    total,
                    tool_name_map.get(tool_use_id, "unknown"),
                    tool_use_id,
                ))
                continue
            if block_type == "tool_use":
                records.append(_make_record(
                    msg_idx,
                    block_idx,
                    role,
                    "tool_use",
                    _block_text(block),
                    total,
                    str(_block_attr(block, "name", "unknown") or "unknown"),
                    str(_block_attr(block, "id", "") or ""),
                ))
                continue
            kind = f"{role}_text" if block_type == "text" and role in ("user", "assistant") else block_type or "message_block"
            records.append(_make_record(msg_idx, block_idx, role, kind, _block_text(block), total))
    return [record for record in records if record["text"]]


def _focus_terms(focus: str) -> list[str]:
    focus = (focus or "").strip()
    if not focus:
        return []
    terms = [focus.lower()]
    terms.extend(re.findall(r"[A-Za-z0-9_./:-]{3,}", focus.lower()))
    return list(dict.fromkeys(terms))


def apply_rule_scores(records: list[dict], focus: str = "") -> None:
    last_user_text_idx = max(
        (record["message_index"] for record in records if record["role"] == "user" and record["kind"] == "user_text"),
        default=-1,
    )
    focus_terms = _focus_terms(focus)
    for record in records:
        text = record["text"]
        lowered = text.lower()
        score = 0
        reasons = []
        protected = False

        if record["kind"] == "user_text":
            score += 2
            reasons.append("user_text")
        elif record["kind"] == "assistant_text":
            score += 1
            reasons.append("assistant_text")
        elif record["kind"] == "tool_use":
            score += 1
            reasons.append("tool_use")

        if record["is_recent"]:
            score += 4
            protected = True
            reasons.append("recent")

        if record["message_index"] == last_user_text_idx and record["kind"] == "user_text":
            score += 5
            protected = True
            reasons.append("last_user_request")

        if _contains_any(text, REQUIREMENT_KEYWORDS):
            score += 4
            if record["role"] == "user":
                protected = True
            reasons.append("requirement_or_decision")

        if _contains_any(text, PROTOCOL_KEYWORDS):
            score += 5
            protected = True
            reasons.append("protocol")

        if _contains_any(text, ERROR_KEYWORDS):
            score += 5
            protected = True
            reasons.append("error_or_blocker")

        if _contains_any(text, STATE_KEYWORDS):
            score += 3
            reasons.append("state_or_todo")

        if FILE_REF_RE.search(text) or SYMBOL_RE.search(text):
            score += 2
            reasons.append("code_reference")

        if focus_terms and any(term in lowered for term in focus_terms):
            score += 3
            reasons.append("focus_match")

        if record["kind"] == "tool_result":
            if "[Previous: used " in text:
                score -= 4
                reasons.append("old_tool_placeholder")
            if record["char_count"] > 3000 and not _contains_any(text, ERROR_KEYWORDS):
                score -= 2
                reasons.append("long_tool_output")
            if record.get("tool_name") == "read_inbox" and text.strip() in ("[]", "(no output)"):
                score -= 4
                reasons.append("empty_inbox_result")

        record["rule_score"] = score
        record["final_score"] = score
        record["reasons"] = reasons
        record["protected"] = protected


def _extract_response_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if hasattr(block, "text"):
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def _parse_json_object(text: str) -> dict:
    """Parse JSON from text, trying to extract the first JSON object if direct parse fails.

    Returns the parsed dict, or raises json.JSONDecodeError if no valid JSON object found.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            raise


def select_llm_scoring_candidates(records: list[dict]) -> list[dict]:
    eligible = [
        record for record in records
        if record["text"] and (
            record["protected"] or record["is_recent"] or record["rule_score"] > 0
        )
    ]
    ranked = sorted(
        eligible,
        key=lambda r: (
            bool(r["protected"]),
            int(r["rule_score"]),
            bool(r["is_recent"]),
            int(r["message_index"]),
        ),
        reverse=True,
    )
    selected = ranked[:COMPACT_LLM_CANDIDATE_LIMIT]
    return sorted(selected, key=lambda r: (r["message_index"], r["block_index"]))


def _records_payload(records: list[dict], per_record_limit: int, total_budget: int) -> list[dict]:
    payload = []
    used = 0
    for record in records:
        item = {
            "id": record["id"],
            "role": record["role"],
            "kind": record["kind"],
            "tool_name": record.get("tool_name"),
            "rule_score": record["rule_score"],
            "reasons": record["reasons"],
            "text": _truncate_text(record["text"], per_record_limit),
        }
        item_size = len(_json_dumps(item))
        if payload and used + item_size > total_budget:
            break
        payload.append(item)
        used += item_size
    return payload


def score_records_with_weak_llm(records: list[dict], focus: str = "") -> dict[str, dict]:
    if not COMPACT_USE_WEAK_LLM_SCORING or not records:
        return {}
    payload = _records_payload(
        records,
        per_record_limit=min(1200, COMPACT_MAX_RECORD_CHARS),
        total_budget=max(8000, COMPACT_INPUT_CHAR_BUDGET // 2),
    )
    if not payload:
        return {}
    prompt = (
        "Score these conversation records for context compression.\n"
        "Return only valid JSON in this shape:\n"
        "{\"scores\":[{\"id\":\"m1.b0\",\"score\":0,\"category\":\"noise\",\"reason\":\"short reason\"}]}\n\n"
        "Use score 5 for explicit user requirements, confirmed decisions, constraints, current state, "
        "open tasks, critical errors, approvals, or blockers. Use score 0 for noise, repeated polling, "
        "old tool placeholders, or unimportant raw output. Rule scores are hints, not final answers. "
        "Do not omit ids from the input.\n\n"
        f"<focus>\n{focus or '(none)'}\n</focus>\n\n"
        f"<records>\n{_json_dumps(payload)}\n</records>"
    )
    try:
        profile = get_model_profile("weak")
        response = profile.client.messages.create(
            model=profile.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
        )
        raw = _parse_json_object(_extract_response_text(response.content))
    except Exception as exc:
        print(f"[Traceback: weak scoring JSON parse failed: {exc}]")
        print("[auto_compact] Degrading to pure-rule scoring (no LLM scores).")
        return {}

    scores = {}
    for item in raw.get("scores", []):
        if not isinstance(item, dict):
            continue
        record_id = str(item.get("id") or "")
        if not record_id:
            continue
        try:
            score = int(item.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        scores[record_id] = {
            "score": max(0, min(score, 5)),
            "category": str(item.get("category") or "uncategorized"),
            "reason": str(item.get("reason") or ""),
        }
    return scores


def apply_llm_scores(records: list[dict], llm_scores: dict[str, dict]) -> None:
    for record in records:
        llm = llm_scores.get(record["id"])
        if not llm:
            continue
        record["llm_score"] = llm["score"]
        record["final_score"] = record["rule_score"] + (llm["score"] * 2)
        record["reasons"].append(f"weak_llm:{llm['category']}")
        if llm["score"] >= 4 and record["role"] == "user":
            record["protected"] = True


def select_recent_records(records: list[dict]) -> list[dict]:
    return sorted(
        [record for record in records if record["is_recent"]],
        key=lambda r: (r["message_index"], r["block_index"]),
    )


def select_important_records(records: list[dict], recent_records: list[dict]) -> list[dict]:
    recent_ids = {record["id"] for record in recent_records}
    older = [record for record in records if record["id"] not in recent_ids]
    selected = {}

    def add(record: dict):
        if len(selected) >= COMPACT_MAX_IMPORTANT_RECORDS:
            return
        selected.setdefault(record["id"], record)

    for record in sorted(older, key=lambda r: (r["message_index"], r["block_index"])):
        if record["protected"]:
            add(record)

    categories = (
        "requirement_or_decision",
        "protocol",
        "error_or_blocker",
        "state_or_todo",
        "code_reference",
        "focus_match",
    )
    for category in categories:
        matches = [
            record for record in older
            if category in record["reasons"] and record["id"] not in selected
        ]
        for record in sorted(matches, key=lambda r: r["final_score"], reverse=True)[:5]:
            add(record)

    high_score = [
        record for record in older
        if record["id"] not in selected and record["final_score"] >= COMPACT_IMPORTANCE_MIN_SCORE
    ]
    for record in sorted(high_score, key=lambda r: r["final_score"], reverse=True):
        add(record)

    return sorted(selected.values(), key=lambda r: (r["message_index"], r["block_index"]))


def _format_records(records: list[dict], char_budget: int) -> str:
    if not records:
        return "(none)"
    chunks = []
    used = 0
    for record in records:
        score = record.get("final_score", record.get("rule_score", 0))
        header = (
            f"[{record['id']}] role={record['role']} kind={record['kind']} "
            f"tool={record.get('tool_name') or '-'} score={score} "
            f"reasons={','.join(record.get('reasons') or []) or '-'}"
        )
        text_budget = min(COMPACT_MAX_RECORD_CHARS, max(500, char_budget // max(1, len(records))))
        body = _truncate_text(record["text"], text_budget)
        chunk = f"{header}\n{body}"
        if used + len(chunk) > char_budget:
            remaining = char_budget - used - len(header) - 20
            if remaining < 300:
                break
            chunk = f"{header}\n{_truncate_text(record['text'], remaining)}"
        chunks.append(chunk)
        used += len(chunk)
    return "\n\n---\n\n".join(chunks)


def summarize_with_strong_llm(
    previous_summary: str,
    important_records: list[dict],
    recent_records: list[dict],
    transcript_path,
    critical: str | None,
    focus: str = "",
) -> str:
    budget = max(20000, COMPACT_INPUT_CHAR_BUDGET)
    previous_budget = max(4000, budget // 4)
    important_budget = max(8000, budget // 2)
    recent_budget = max(4000, budget // 4)
    prompt = (
        "Create an updated continuity summary for an agent conversation after compression.\n"
        "Merge the previous summary with the important records and recent context. "
        "Newer decisions override older conflicting decisions. Preserve explicit user requirements, "
        "confirmed decisions, current state, open tasks, important files/functions/config, critical errors, "
        "blockers, and next steps. Treat this as memory reconstruction, not as a new user request. "
        "Do not convert remembered constraints, risks, or blockers into action items unless the records "
        "explicitly ask the agent to act on them now. Drop repetitive polling, raw dumps, and stale tool output.\n\n"
        "Return concise plain text with these sections when relevant: User requirements, Confirmed decisions, "
        "Remembered constraints or risks, Current state, Important files or symbols, Errors or blockers, "
        "Open tasks, Next steps. Put an item under Open tasks or Next steps only when it is explicitly "
        "an unfinished action, not merely something the user asked to remember.\n\n"
        f"<transcript_path>\n{transcript_path}\n</transcript_path>\n\n"
        f"<focus>\n{focus or '(none)'}\n</focus>\n\n"
        f"<previous_summary>\n{_truncate_text(previous_summary, previous_budget) or '(none)'}\n</previous_summary>\n\n"
        f"<important_records>\n{_format_records(important_records, important_budget)}\n</important_records>\n\n"
        f"<recent_records>\n{_format_records(recent_records, recent_budget)}\n</recent_records>\n\n"
        f"<critical_context>\n{_truncate_text(critical or '', 4000) or '(none)'}\n</critical_context>\n\n"
        f"<todos>\n{TODO.render()}\n</todos>"
    )
    try:
        profile = get_model_profile("strong")
        response = profile.client.messages.create(
            model=profile.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2500,
        )
        summary = _extract_response_text(response.content)
    except Exception as exc:
        print(f"[strong compaction summary failed: {exc}]")
        fallback = (
            f"{previous_summary}\n\n"
            "Important recent context:\n"
            f"{_format_records(important_records, 8000)}\n\n"
            "Recent context:\n"
            f"{_format_records(recent_records, 4000)}"
        )
        summary = _truncate_text(fallback.strip(), 12000)
    return summary or "No summary generated."


# -- Layer 1: micro_compact - replace old tool results with placeholders --
def micro_compact(messages: list) -> list:
    tool_results = []
    for msg_idx, msg in enumerate(messages):

        if msg.get("role") == "user" and isinstance(msg.get("content"), list):

            for part_idx, part in enumerate(msg["content"]):

                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))

    if len(tool_results) <= KEEP_RECENT:
        return messages

    tool_name_map = _build_tool_name_map(messages)

    to_clear = tool_results[:-KEEP_RECENT]

    for _, _, result in to_clear:
        if isinstance(result.get("content"), str) and len(result["content"]) > 100:
            tool_id = result.get("tool_use_id", "")
            tool_name = tool_name_map.get(tool_id, "unknown")
            result["content"] = f"[Previous: used {tool_name}]"

    return messages


# -- Layer 2/3: auto_compact - save transcript, filter importance, summarize, replace messages --
def auto_compact(messages: list, focus: str = "") -> list:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"

    with open(transcript_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(_json_dumps(msg) + "\n")

    print(f"[transcript saved: {transcript_path}]")

    previous_summary = extract_previous_summary(messages)
    records = build_compaction_records(messages)
    apply_rule_scores(records, focus)
    llm_candidates = select_llm_scoring_candidates(records)
    llm_scores = score_records_with_weak_llm(llm_candidates, focus)
    apply_llm_scores(records, llm_scores)
    recent_records = select_recent_records(records)
    important_records = select_important_records(records, recent_records)
    critical = extract_critical(messages)

    summary = summarize_with_strong_llm(
        previous_summary,
        important_records,
        recent_records,
        transcript_path,
        critical,
        focus,
    )
    preserved = f"\n\n<critical_context>\n{critical}\n</critical_context>" if critical else ""

    return [
        {
            "role": "user",
            "content": (
                f"[Conversation compressed. Transcript: {transcript_path}]\n\n"
                f"{summary}"
                f"{preserved}\n\n"
                f"<todos>\n{TODO.render()}\n</todos>"
            )
        },
        {
            "role": "assistant",
            "content": "Understood. I have restored the compressed context. This summary is memory, not a new user request."
        },
    ]
