import threading

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_request",
    "plan_approval_response",
}

shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()
