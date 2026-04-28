import json

from .agent import latest_assistant_text, run_react_loop
from .planner import plan_task
from .prompts import REACT_EXECUTOR_SYSTEM, SYSTEM
from .reflection import reflect_task

COMPLEX_KEYWORDS = (
    "architecture",
    "architect",
    "build",
    "complex",
    "coverage",
    "database",
    "design",
    "end-to-end",
    "feature",
    "migration",
    "multi-step",
    "multiple files",
    "plan-and-execute",
    "refactor",
    "test",
    "架构",
    "重构",
    "复杂",
    "多步",
    "多文件",
    "多个文件",
    "完整",
    "端到端",
    "设计",
    "实现",
    "新增",
    "迁移",
    "数据库",
    "测试",
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
    return any(keyword in lowered or keyword in text for keyword in keywords)


def _needs_reflection(user_request: str) -> bool:
    return _contains_keyword(user_request, REFLECTION_KEYWORDS)


def _needs_plan(user_request: str) -> bool:
    if _needs_reflection(user_request):
        return True
    if len(user_request) > 500:
        return True
    return _contains_keyword(user_request, COMPLEX_KEYWORDS)


def choose_route(user_request: str) -> dict:
    reflection_required = _needs_reflection(user_request)
    plan_required = _needs_plan(user_request)
    return {
        "mode": "plan_execute" if plan_required else "react",
        "reflection_required": reflection_required,
    }


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


def _run_step(
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
        max_tool_rounds=25,
        drain_lead_inbox=False,
    )
    result = latest_assistant_text(step_messages) or "(no textual summary)"
    return {
        "step_id": str(step.get("id") or ""),
        "goal": str(step.get("goal") or ""),
        "repair": repair,
        "deliverable": bool(step.get("deliverable")) and not repair,
        "result": _truncate(result),
    }


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
        result = item.get("result") or ""
        first_line = result.splitlines()[0] if result else "(no summary)"
        summary_lines.append(f"- {label} {item.get('step_id')}: {item.get('goal')} -> {first_line}")

    if reflection:
        verdict = reflection.get("verdict", "block")
        reflection_summary = reflection.get("summary") or "(no summary)"
        summary_lines.append(f"Reflection: {verdict} - {reflection_summary}")
        issues = reflection.get("issues") or []
        for issue in issues[:3]:
            summary_lines.append(f"- issue: {issue}")
    lines.extend(summary_lines)
    return "\n".join(lines)


def orchestrate_task(history: list):
    user_request = _latest_user_text(history)
    route = choose_route(user_request)

    if route["mode"] == "react":
        return run_react_loop(
            history,
            model_profile="weak",
            system_prompt=SYSTEM,
        )

    print(f"[orchestrator] route={route['mode']} reflection={route['reflection_required']}")
    plan = plan_task(user_request, history_excerpt=_history_excerpt(history[:-1]))
    if route["reflection_required"]:
        plan["reflection_required"] = True

    step_results = []
    for step in plan.get("steps", []):
        step_results.append(_run_step(user_request, plan, step, step_results))

    reflection = None
    if plan.get("reflection_required"):
        reflection = reflect_task(user_request, plan, step_results)
        if reflection.get("verdict") == "revise" and reflection.get("repair_steps"):
            for repair_step in reflection["repair_steps"]:
                step_results.append(
                    _run_step(user_request, plan, repair_step, step_results, repair=True)
                )
            reflection = reflect_task(user_request, plan, step_results)

    history.append({
        "role": "assistant",
        "content": [{
            "type": "text",
            "text": _format_final_response(route, plan, step_results, reflection),
        }],
    })
