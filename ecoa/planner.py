import json

from .config import get_model_profile
from .prompts import PLANNER_SYSTEM


def _extract_text(content) -> str:
    parts = []
    for block in content:
        if hasattr(block, "text"):
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def _parse_json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def _fallback_plan(user_request: str, reason: str) -> dict:
    return {
        "mode": "plan_execute",
        "reflection_required": False,
        "rationale": f"Planner fallback: {reason}",
        "steps": [
            {
                "id": "1",
                "goal": "Complete the user request",
                "instructions": user_request,
                "success_criteria": "The request is completed or a clear blocker is reported.",
                "risk": "medium",
            }
        ],
    }


def _normalize_plan(plan: dict, user_request: str) -> dict:
    if not isinstance(plan, dict):
        return _fallback_plan(user_request, "planner returned a non-object response")

    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return _fallback_plan(user_request, "planner returned no executable steps")

    normalized_steps = []
    for index, raw_step in enumerate(steps, start=1):
        if not isinstance(raw_step, dict):
            raw_step = {"goal": str(raw_step)}
        normalized_steps.append({
            "id": str(raw_step.get("id") or index),
            "goal": str(raw_step.get("goal") or "Execute this step"),
            "instructions": str(raw_step.get("instructions") or raw_step.get("goal") or ""),
            "success_criteria": str(raw_step.get("success_criteria") or "Step is complete."),
            "risk": str(raw_step.get("risk") or "medium"),
        })

    return {
        "mode": "plan_execute",
        "reflection_required": bool(plan.get("reflection_required")),
        "rationale": str(plan.get("rationale") or ""),
        "steps": normalized_steps[:12],
    }


def plan_task(user_request: str, history_excerpt: str = "", model_profile="strong") -> dict:
    profile = get_model_profile(model_profile)
    prompt = (
        "Create an execution plan for this user request.\n\n"
        f"<user_request>\n{user_request}\n</user_request>\n\n"
        f"<recent_history>\n{history_excerpt or '(none)'}\n</recent_history>"
    )
    response = profile.client.messages.create(
        model=profile.model,
        system=PLANNER_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3000,
    )
    text = _extract_text(response.content)
    try:
        plan = _parse_json_object(text)
    except Exception as exc:
        return _fallback_plan(user_request, str(exc))
    return _normalize_plan(plan, user_request)
