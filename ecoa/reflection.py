import json

from .config import get_model_profile
from .planner import _extract_text, _parse_json_object
from .prompts import REFLECTION_SYSTEM


def _fallback_reflection(reason: str) -> dict:
    return {
        "verdict": "block",
        "summary": f"Reflection fallback: {reason}",
        "issues": [reason],
        "repair_steps": [],
    }


def _normalize_reflection(reflection: dict) -> dict:
    if not isinstance(reflection, dict):
        return _fallback_reflection("reflection returned a non-object response")

    verdict = str(reflection.get("verdict") or "block").lower()
    if verdict not in ("pass", "revise", "block"):
        verdict = "block"

    repair_steps = []
    for index, raw_step in enumerate(reflection.get("repair_steps") or [], start=1):
        if not isinstance(raw_step, dict):
            raw_step = {"goal": str(raw_step)}
        repair_steps.append({
            "id": str(raw_step.get("id") or f"r{index}"),
            "goal": str(raw_step.get("goal") or "Repair issue"),
            "instructions": str(raw_step.get("instructions") or raw_step.get("goal") or ""),
            "success_criteria": str(raw_step.get("success_criteria") or "Repair is complete."),
            "risk": str(raw_step.get("risk") or "medium"),
        })

    return {
        "verdict": verdict,
        "summary": str(reflection.get("summary") or ""),
        "issues": [str(issue) for issue in reflection.get("issues") or []],
        "repair_steps": repair_steps[:6],
    }


def reflect_task(
    user_request: str,
    plan: dict,
    step_results: list,
    model_profile="strong",
) -> dict:
    profile = get_model_profile(model_profile)
    prompt = (
        "Review the completed task.\n\n"
        f"<user_request>\n{user_request}\n</user_request>\n\n"
        f"<plan>\n{json.dumps(plan, indent=2, ensure_ascii=False)}\n</plan>\n\n"
        f"<execution_results>\n{json.dumps(step_results, indent=2, ensure_ascii=False)}\n</execution_results>"
    )
    response = profile.client.messages.create(
        model=profile.model,
        system=REFLECTION_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=3000,
    )
    text = _extract_text(response.content)
    try:
        reflection = _parse_json_object(text)
    except Exception as exc:
        return _fallback_reflection(str(exc))
    return _normalize_reflection(reflection)
