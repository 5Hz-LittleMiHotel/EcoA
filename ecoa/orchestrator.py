import json
import re
import time

from .agent import latest_assistant_text, run_react_loop
from .config import get_model_profile
from .message_bus import BUS
from .planner import plan_task
from .prompts import DIRECT_EXECUTOR_SYSTEM, REACT_EXECUTOR_SYSTEM, ROUTER_SYSTEM
from .reflection import reflect_task
from .task_board import TASKS
from .teammates import TEAM
from .tool_registry import EXECUTOR_TOOL_HANDLERS, EXECUTOR_TOOLS
from .worktrees import WORKTREES

FOLLOWUP_KEYWORDS = (
    "continue",
    "expand",
    "it",
    "that",
    "the above",
    "this",
    "where",
    "why",
    "your",
    "上面",
    "刚才",
    "继续",
    "展开",
    "这个",
    "那个",
    "它",
    "哪里",
    "在哪",
    "为什么",
)

DIRECT_REACT_KEYWORDS = (
    "directly output",
    "do not modify",
    "do not write",
    "don't modify",
    "don't plan",
    "just answer",
    "just explain",
    "no code changes",
    "only explain",
    "read only",
    "直接输出",
    "不要规划",
    "不要修改",
    "不要写入",
    "不修改代码",
    "只分析",
    "只解释",
    "只输出",
    "只读",
)

EXPLICIT_PLAN_KEYWORDS = (
    "multi-step",
    "plan-and-execute",
    "plan and execute",
    "step by step",
    "use planner",
    "分步骤",
    "多步",
    "先规划",
    "规划后执行",
    "计划并执行",
)

MODIFY_KEYWORDS = (
    "add",
    "change",
    "delete",
    "edit",
    "fix",
    "implement",
    "improve",
    "migrate",
    "modify",
    "refactor",
    "remove",
    "rewrite",
    "update",
    "write",
    "increase",
    "修复",
    "修改",
    "实现",
    "新增",
    "增加",
    "改进",
    "添加",
    "编辑",
    "删除",
    "移除",
    "重构",
    "迁移",
    "写入",
    "更新",
)

READONLY_KEYWORDS = (
    "analyze",
    "describe",
    "explain",
    "inspect",
    "read",
    "summarize",
    "分析",
    "说明",
    "解释",
    "阅读",
    "查看",
    "总结",
)

COMPLEX_SCOPE_KEYWORDS = (
    "architecture",
    "build",
    "complex",
    "coverage",
    "database",
    "end-to-end",
    "feature",
    "migration",
    "multiple files",
    "refactor",
    "test suite",
    "架构",
    "复杂",
    "多文件",
    "多个文件",
    "完整",
    "端到端",
    "数据库",
    "全局",
)

REFLECTION_KEYWORDS = (
    "auth",
    "authentication",
    "authorization",
    "crypto",
    "delete",
    "deploy",
    "payment",
    "production",
    "release",
    "review",
    "security",
    "审查",
    "生产级",
    "高要求",
    "安全",
    "权限",
    "认证",
    "支付",
    "删除",
    "发布",
    "部署",
    "加密",
)


def _latest_user_text(history: list) -> str:
    for msg in reversed(history):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            return json.dumps(content, ensure_ascii=False, default=str)
    return ""


def _message_text(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False, default=str)


def _history_excerpt(history: list, limit: int = 6000) -> str:
    items = []
    for msg in history[-8:]:
        role = msg.get("role", "unknown")
        items.append(f"{role}: {_message_text(msg)}")
    text = "\n\n".join(items)
    return text[-limit:]


def _contains_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    for keyword in keywords:
        keyword_lower = keyword.lower()
        if keyword.isascii() and keyword.replace("_", "").isalnum():
            if re.search(rf"\b{re.escape(keyword_lower)}\b", lowered):
                return True
            continue
        if keyword_lower in lowered or keyword in text:
            return True
    return False


def _has_modify_intent(user_request: str) -> bool:
    lowered = user_request.lower()
    for phrase in DIRECT_REACT_KEYWORDS:
        lowered = lowered.replace(phrase.lower(), " ")
    return _contains_keyword(lowered, MODIFY_KEYWORDS)


def _needs_reflection(user_request: str) -> bool:
    return _contains_keyword(user_request, REFLECTION_KEYWORDS)


def _decision(
    mode: str,
    reflection_required: bool,
    intent: str,
    reason: str,
    confidence: str,
    needs_llm: bool,
    signals: dict | None = None,
    source: str = "rules",
) -> dict:
    return {
        "mode": mode,
        "reflection_required": reflection_required,
        "intent": intent,
        "reason": reason,
        "confidence": confidence,
        "needs_llm": needs_llm,
        "signals": signals or {},
        "source": source,
    }


def _is_short_followup(user_request: str) -> bool:
    if len(user_request.strip()) > 160:
        return False
    if _has_modify_intent(user_request):
        return False
    if _contains_keyword(user_request, EXPLICIT_PLAN_KEYWORDS):
        return False
    if _needs_reflection(user_request):
        return False
    return _contains_keyword(user_request, FOLLOWUP_KEYWORDS)


def _parse_json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def _extract_text(content) -> str:
    parts = []
    for block in content:
        if hasattr(block, "text"):
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def _normalize_llm_route(raw: dict, fallback: dict) -> dict:
    if not isinstance(raw, dict):
        return fallback
    mode = str(raw.get("mode") or fallback["mode"]).lower()
    if mode not in ("react", "plan_execute"):
        mode = fallback["mode"]
    intent = str(raw.get("intent") or fallback.get("intent") or "unknown").lower()
    if intent not in ("followup", "analysis", "modify", "review", "unknown"):
        intent = fallback.get("intent", "unknown")
    confidence = str(raw.get("confidence") or "medium").lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "medium"
    reflection_required = raw.get("reflection_required")
    if reflection_required is None:
        reflection_required = fallback.get("reflection_required", False)
    reflection_required = bool(reflection_required)
    if reflection_required:
        mode = "plan_execute"
    return {
        "mode": mode,
        "reflection_required": reflection_required,
        "intent": intent,
        "reason": str(raw.get("reason") or fallback.get("reason") or "LLM route"),
        "confidence": confidence,
        "needs_llm": False,
        "signals": fallback.get("signals", {}),
        "source": "llm_router",
        "rule_candidate": {
            "mode": fallback.get("mode"),
            "reflection_required": fallback.get("reflection_required"),
            "intent": fallback.get("intent"),
            "reason": fallback.get("reason"),
            "confidence": fallback.get("confidence"),
        },
    }


def _route_with_llm(user_request: str, rule_route: dict, history_excerpt: str = "") -> dict:
    profile = get_model_profile("strong")
    prompt = (
        "Classify this request for EcoA's orchestrator.\n\n"
        f"<user_request>\n{user_request}\n</user_request>\n\n"
        f"<recent_history>\n{history_excerpt or '(none)'}\n</recent_history>\n\n"
        f"<rule_candidate>\n{json.dumps(rule_route, indent=2, ensure_ascii=False)}\n</rule_candidate>"
    )
    response = profile.client.messages.create(
        model=profile.model,
        system=ROUTER_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1000,
    )
    text = _extract_text(response.content)
    return _normalize_llm_route(_parse_json_object(text), rule_route)


def _choose_route_by_rules(user_request: str) -> dict:
    explicit_plan = _contains_keyword(user_request, EXPLICIT_PLAN_KEYWORDS)
    explicit_react = _contains_keyword(user_request, DIRECT_REACT_KEYWORDS)
    reflection_required = _needs_reflection(user_request)
    modify_intent = _has_modify_intent(user_request)
    readonly_intent = _contains_keyword(user_request, READONLY_KEYWORDS)
    complex_scope = _contains_keyword(user_request, COMPLEX_SCOPE_KEYWORDS)
    signals = {
        "explicit_plan": explicit_plan,
        "explicit_react": explicit_react,
        "reflection_required": reflection_required,
        "modify_intent": modify_intent,
        "readonly_intent": readonly_intent,
        "complex_scope": complex_scope,
        "length": len(user_request.strip()),
    }

    if _is_short_followup(user_request):
        return _decision(
            "react",
            False,
            "followup",
            "short follow-up without modification or review intent",
            "high",
            False,
            signals,
        )

    if explicit_react and (modify_intent or explicit_plan):
        return _decision(
            "react",
            False,
            "unknown",
            "conflicting direct-response and modification/planning signals",
            "low",
            True,
            signals,
        )

    if explicit_react and not modify_intent and not reflection_required and not explicit_plan:
        return _decision(
            "react",
            False,
            "analysis",
            "user requested a direct/read-only response",
            "high",
            False,
            signals,
        )

    if reflection_required:
        return _decision(
            "plan_execute",
            True,
            "review" if readonly_intent and not modify_intent else "modify",
            "review, production, or risk keyword requires task-level reflection",
            "high",
            False,
            signals,
        )

    if explicit_plan:
        return _decision(
            "plan_execute",
            False,
            "modify" if modify_intent else "analysis",
            "user explicitly asked for multi-step planning",
            "high",
            False,
            signals,
        )

    if modify_intent:
        if complex_scope or len(user_request) > 500:
            return _decision(
                "plan_execute",
                False,
                "modify",
                "modification request has complex scope",
                "high",
                False,
                signals,
            )
        return _decision(
            "react",
            False,
            "modify",
            "small modification can be handled by one ReAct pass",
            "high",
            False,
            signals,
        )

    if readonly_intent:
        if complex_scope and len(user_request) > 500:
            return _decision(
                "plan_execute",
                False,
                "analysis",
                "large read-only analysis request has complex scope",
                "medium",
                True,
                signals,
            )
        return _decision(
            "react",
            False,
            "analysis",
            "read-only analysis does not need planning",
            "high",
            False,
            signals,
        )

    if complex_scope and len(user_request) > 500:
        return _decision(
            "plan_execute",
            False,
            "unknown",
            "large request with complex-scope keywords",
            "medium",
            True,
            signals,
        )

    return _decision(
        "react",
        False,
        "unknown",
        "default route",
        "medium" if len(user_request.strip()) > 160 else "high",
        len(user_request.strip()) > 160,
        signals,
    )


def choose_route(
    user_request: str,
    history_excerpt: str = "",
    use_llm_router: bool = False,
) -> dict:
    route = _choose_route_by_rules(user_request)
    if not use_llm_router or not route.get("needs_llm"):
        return route
    try:
        return _route_with_llm(user_request, route, history_excerpt)
    except Exception as exc:
        route = dict(route)
        route["source"] = "rules_fallback"
        route["needs_llm"] = False
        route["router_error"] = str(exc)
        return route


def _truncate(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... (truncated)"


def _format_step_prompt(
    user_request: str,
    plan: dict,
    step: dict,
    prior_results: list,
    repair: bool = False,
) -> str:
    assignment = "repair step" if repair else "plan step"
    return (
        f"You are executing one {assignment} with weak ReAct.\n\n"
        f"<original_user_request>\n{user_request}\n</original_user_request>\n\n"
        f"<overall_plan>\n{json.dumps(plan, indent=2, ensure_ascii=False)}\n</overall_plan>\n\n"
        f"<current_step>\n{json.dumps(step, indent=2, ensure_ascii=False)}\n</current_step>\n\n"
        f"<prior_results>\n{json.dumps(prior_results, indent=2, ensure_ascii=False)}\n</prior_results>\n\n"
        "Complete only the current step. Use tools as needed. When done, summarize "
        "what changed, important files, verification performed, and any blocker."
    )


def _step_result(
    step: dict,
    result: str,
    repair: bool = False,
    error: bool = False,
) -> dict:
    return {
        "step_id": str(step.get("id") or ""),
        "type": str(step.get("type") or "react_step"),
        "goal": str(step.get("goal") or ""),
        "repair": repair,
        "error": error,
        "deliverable": bool(step.get("deliverable")) and not repair,
        "result": _truncate(str(result)),
    }


def _run_react_step(
    user_request: str,
    plan: dict,
    step: dict,
    prior_results: list,
    repair: bool = False,
) -> dict:
    step_messages = [{
        "role": "user",
        "content": _format_step_prompt(user_request, plan, step, prior_results, repair),
    }]
    run_react_loop(
        step_messages,
        model_profile="weak",
        system_prompt=REACT_EXECUTOR_SYSTEM,
        tools=EXECUTOR_TOOLS,
        tool_handlers=EXECUTOR_TOOL_HANDLERS,
        max_tool_rounds=25,
        drain_lead_inbox=False,
    )
    result = latest_assistant_text(step_messages) or "(no textual summary)"
    return _step_result(step, result, repair=repair)


def _wait_for_teammate(args: dict) -> str:
    from_name = str(args.get("from") or args.get("name") or "").strip()
    timeout_seconds = int(args.get("timeout_seconds") or 0)
    timeout_seconds = max(0, min(timeout_seconds, 120))
    deadline = time.time() + timeout_seconds
    collected = []
    while True:
        inbox = BUS.read_inbox("lead")
        unmatched = []
        for msg in inbox:
            if not from_name or msg.get("from") == from_name:
                collected.append(msg)
            else:
                unmatched.append(msg)
        if unmatched:
            BUS.requeue_inbox("lead", unmatched)
        if collected or time.time() >= deadline:
            return json.dumps(collected, indent=2, ensure_ascii=False) if collected else "No matching teammate messages."
        time.sleep(2)


def _run_orchestrator_step(step: dict) -> dict:
    step_type = str(step.get("type") or "react_step")
    args = step.get("args") if isinstance(step.get("args"), dict) else {}
    try:
        if step_type == "spawn_teammate":
            output = TEAM.spawn(
                str(args["name"]),
                str(args.get("role") or "teammate"),
                str(args.get("prompt") or step.get("instructions") or step.get("goal") or ""),
            )
        elif step_type == "send_message":
            output = BUS.send(
                "lead",
                str(args["to"]),
                str(args.get("content") or step.get("instructions") or ""),
                "message",
            )
            wake = TEAM.wake(str(args["to"]))
            if not wake.startswith("Error:"):
                output = f"{output}; {wake}"
        elif step_type == "wait_teammate":
            output = _wait_for_teammate(args)
        elif step_type == "task_get":
            output = TASKS.get(int(args["task_id"]))
        elif step_type == "task_create":
            output = TASKS.create(str(args["subject"]), str(args.get("description") or ""))
        elif step_type == "task_update":
            output = TASKS.update(
                int(args["task_id"]),
                args.get("status"),
                args.get("owner"),
                args.get("addBlockedBy"),
                args.get("removeBlockedBy"),
            )
        elif step_type == "task_list":
            output = TASKS.list_all()
        elif step_type == "worktree_create":
            task_id = args.get("task_id")
            output = WORKTREES.create(
                str(args["name"]),
                int(task_id) if task_id is not None else None,
                args.get("base_ref", "HEAD"),
            )
        elif step_type == "worktree_status":
            output = WORKTREES.status(str(args["name"]))
        elif step_type == "worktree_run":
            output = WORKTREES.run(str(args["name"]), str(args["command"]))
        elif step_type == "worktree_keep":
            output = WORKTREES.keep(str(args["name"]))
        elif step_type == "worktree_remove":
            output = WORKTREES.remove(
                str(args["name"]),
                bool(args.get("force", False)),
                bool(args.get("complete_task", False)),
            )
        elif step_type == "worktree_list":
            output = WORKTREES.list_all()
        else:
            output = f"Unknown orchestrator step type: {step_type}"
            return _step_result(step, output, error=True)
        return _step_result(step, output)
    except Exception as exc:
        return _step_result(step, f"Error: {exc}", error=True)


def _execute_plan_step(
    user_request: str,
    plan: dict,
    step: dict,
    prior_results: list,
    repair: bool = False,
) -> dict:
    if repair or str(step.get("type") or "react_step") == "react_step":
        return _run_react_step(user_request, plan, step, prior_results, repair=repair)
    return _run_orchestrator_step(step)


def _select_deliverable_results(step_results: list) -> list:
    explicit = [
        item for item in step_results
        if item.get("deliverable") and item.get("result")
    ]
    if explicit:
        return explicit

    for item in reversed(step_results):
        if not item.get("repair") and item.get("result"):
            return [item]
    return step_results[-1:] if step_results else []


def _format_final_response(
    route: dict,
    plan: dict,
    step_results: list,
    reflection: dict | None,
) -> str:
    deliverables = _select_deliverable_results(step_results)
    lines = []
    for item in deliverables:
        result = (item.get("result") or "").strip()
        if result:
            lines.append(result)

    summary_lines = []
    if lines:
        summary_lines.append("---")
    summary_lines.extend([
        f"Completed via {route['mode']}.",
        f"Steps executed: {len(step_results)}.",
    ])
    if plan.get("rationale"):
        summary_lines.append(f"Plan rationale: {plan['rationale']}")

    for item in step_results:
        label = "repair" if item.get("repair") else "step"
        step_type = item.get("type", "react_step")
        result = item.get("result") or ""
        first_line = result.splitlines()[0] if result else "(no summary)"
        summary_lines.append(
            f"- {label} {item.get('step_id')} ({step_type}): {item.get('goal')} -> {first_line}"
        )

    if reflection:
        verdict = reflection.get("verdict", "block")
        reflection_summary = reflection.get("summary") or "(no summary)"
        summary_lines.append(f"Reflection: {verdict} - {reflection_summary}")
        issues = reflection.get("issues") or []
        for issue in issues[:3]:
            summary_lines.append(f"- issue: {issue}")
    lines.extend(summary_lines)
    return "\n".join(lines)


def process_orchestrator_inbox(history: list) -> bool:
    inbox = BUS.read_inbox("lead")
    if not inbox:
        return False

    lines = ["Orchestrator inbox:"]
    for msg in inbox:
        msg_type = msg.get("type", "message")
        sender = msg.get("from", "(unknown)")
        if msg_type == "plan_approval_request":
            lines.append(
                f"- plan approval request {msg.get('request_id', '(missing id)')} from {sender}: "
                f"{msg.get('plan') or msg.get('content', '')}"
            )
        elif msg_type == "shutdown_response":
            lines.append(
                f"- shutdown response from {sender}: approve={msg.get('approve')} "
                f"request_id={msg.get('request_id', '')}"
            )
        else:
            lines.append(f"- {msg_type} from {sender}: {msg.get('content', '')}")

    history.append({
        "role": "assistant",
        "content": [{"type": "text", "text": "\n".join(lines)}],
    })
    return True


def orchestrate_task(history: list):
    user_request = _latest_user_text(history)
    route = choose_route(
        user_request,
        history_excerpt=_history_excerpt(history[:-1]),
        use_llm_router=True,
    )
    print(
        "[orchestrator] "
        f"route={route['mode']} "
        f"reflection={route['reflection_required']} "
        f"intent={route.get('intent', 'unknown')} "
        f"source={route.get('source', 'rules')} "
        f"confidence={route.get('confidence', '')} "
        f"reason={route.get('reason', '')}"
    )
    if route.get("router_error"):
        print(f"[orchestrator] router_fallback={route['router_error']}")

    if route["mode"] == "react":
        return run_react_loop(
            history,
            model_profile="weak",
            system_prompt=DIRECT_EXECUTOR_SYSTEM,
            tools=EXECUTOR_TOOLS,
            tool_handlers=EXECUTOR_TOOL_HANDLERS,
            drain_lead_inbox=False,
        )

    plan = plan_task(user_request, history_excerpt=_history_excerpt(history[:-1]))
    if route["reflection_required"]:
        plan["reflection_required"] = True

    step_results = []
    for step in plan.get("steps", []):
        step_results.append(_execute_plan_step(user_request, plan, step, step_results))

    reflection = None
    if plan.get("reflection_required"):
        reflection = reflect_task(user_request, plan, step_results)
        if reflection.get("verdict") == "revise" and reflection.get("repair_steps"):
            for repair_step in reflection["repair_steps"]:
                step_results.append(
                    _execute_plan_step(user_request, plan, repair_step, step_results, repair=True)
                )
            reflection = reflect_task(user_request, plan, step_results)

    history.append({
        "role": "assistant",
        "content": [{
            "type": "text",
            "text": _format_final_response(route, plan, step_results, reflection),
        }],
    })
