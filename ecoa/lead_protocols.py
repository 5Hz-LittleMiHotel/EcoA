import json
import uuid

from .message_bus import BUS
from .protocols import _tracker_lock, plan_requests, shutdown_requests
from .teammates import TEAM


def _resolve_next_action(approve: bool, next_action: str = "") -> str:
    if approve:
        action = next_action or "proceed"
        if action != "proceed":
            raise ValueError("approve=true requires next_action='proceed'")
        return "proceed"

    action = next_action or "stop"
    if action not in ("revise", "stop"):
        raise ValueError("approve=false requires next_action='revise' or 'stop'")
    return action


def _plan_excerpt(plan: str, limit: int = 1200) -> str:
    text = (plan or "").strip()
    if len(text) <= limit:
        return text or "(empty plan)"
    return text[:limit].rstrip() + "\n... (truncated)"


def _format_plan_receipt(
    request_id: str,
    req: dict,
    approve: bool,
    next_action: str,
    feedback: str,
) -> str:
    decision = "approved" if approve else "rejected"
    if next_action == "proceed":
        continuation = "yes - teammate may proceed with this plan"
    elif next_action == "revise":
        continuation = "yes - teammate must revise and submit a new plan"
    else:
        continuation = "no - teammate must stop this task and return to idle"

    return (
        "Plan Review Receipt\n"
        f"- Teammate: {req.get('from', '(unknown)')}\n"
        f"- Request ID: {request_id}\n"
        f"- Decision: {decision}\n"
        f"- Next action: {next_action}\n"
        f"- Plan continues: {continuation}\n"
        f"- Feedback: {feedback or '(none)'}\n"
        "- Plan:\n"
        f"{_plan_excerpt(req.get('plan', ''))}"
    )


# -- Lead-specific protocol handlers --
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}' (status: pending)"


def handle_plan_review(
    request_id: str,
    approve: bool,
    feedback: str = "",
    next_action: str = "",
) -> str:
    try:
        resolved_action = _resolve_next_action(approve, next_action)
    except ValueError as e:
        return f"Error: {e}"

    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        # FIX: Rebuild missing in-memory plan request from persisted waiting state.
        req = TEAM._find_pending_plan_request(request_id)
        if not req:
            return f"Error: Unknown plan request_id '{request_id}'"
        with _tracker_lock:
            plan_requests[request_id] = req
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
        req["next_action"] = resolved_action
        req["feedback"] = feedback
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {
            "request_id": request_id,
            "approve": approve,
            "feedback": feedback,
            "next_action": resolved_action,
        },
    )
    return _format_plan_receipt(request_id, req, approve, resolved_action, feedback)


def _check_shutdown_status(request_id: str) -> str:
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))

