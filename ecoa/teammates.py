import json
import threading
import time
import uuid
from pathlib import Path

from .config import IDLE_TIMEOUT, POLL_INTERVAL, TEAM_DIR, WORKDIR, get_model_profile
from .file_tools import _run_bash, _run_edit, _run_read, _run_write
from .message_bus import BUS
from .protocols import _tracker_lock, plan_requests, shutdown_requests
from .task_board import TASKS, claim_task, scan_unclaimed_tasks

# -- Identity re-injection after compression --
def make_identity_block(name: str, role: str, team_name: str) -> dict:
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


# -- TeammateManager: persistent named agents with config.json --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}

    def _default_config(self) -> dict:
        return {"team_name": "default", "members": []}

    def _write_config(self, config: dict):
        tmp_path = self.config_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(self.config_path)

    def _normalize_loaded_config(self, config: dict) -> dict:
        changed = False
        for member in config.get("members", []):
            status = member.get("status", "shutdown")
            if status in ("working", "idle"):
                member["status"] = "idle"
                changed = True
            if member.get("status") == "shutdown":
                member.pop("requires_initial_plan", None)
                member.pop("pre_plan_tool_count", None)
            member.setdefault("role", "")
        if changed:
            self._write_config(config)
        return config

    def _prompt_requires_initial_plan(self, prompt: str) -> bool:
        text = prompt.lower()
        return "plan" in text or "approval" in text or "approve" in text

    def _set_initial_plan_required(self, name: str, required: bool):
        member = self._find_member(name)
        if not member:
            return
        if required:
            member["requires_initial_plan"] = True
            member["pre_plan_tool_count"] = 0
        else:
            member.pop("requires_initial_plan", None)
            member.pop("pre_plan_tool_count", None)
        self._save_config()

    def _check_initial_plan_gate(self, name: str, tool_name: str) -> str | None:
        member = self._find_member(name)
        if not member or not member.get("requires_initial_plan"):
            return None
        if self._is_waiting_approval(name) or tool_name == "plan_approval":
            return None

        count = int(member.get("pre_plan_tool_count", 0)) + 1
        member["pre_plan_tool_count"] = count
        self._save_config()
        if count <= 6:
            return None
        return (
            "Error: this task requires an initial plan. Stop inspecting files and "
            "call the plan_approval tool now with a concise plan. Do not continue "
            "other work until the lead approves or rejects it."
        )

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            config = self._default_config()
            self._write_config(config)
            return config

        raw = self.config_path.read_text(encoding="utf-8").strip()
        if not raw:
            config = self._default_config()
            self._write_config(config)
            return config

        try:
            config = json.loads(raw)
        except json.JSONDecodeError:
            backup = self.config_path.with_name(
                f"{self.config_path.name}.bad.{int(time.time())}"
            )
            self.config_path.replace(backup)
            config = self._default_config()
            self._write_config(config)
            return config

        if not isinstance(config, dict):
            config = self._default_config()
            self._write_config(config)
            return config
        config.setdefault("team_name", "default")
        config.setdefault("members", [])
        return self._normalize_loaded_config(config)

    def _save_config(self):
        self._write_config(self.config)

    def _find_member(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        member = self._find_member(name)
        if member:
            if member.get("status") == "shutdown" and status in ("idle", "working"):
                return
            member["status"] = status
            self._save_config()

    def _set_plan_waiting(self, name: str, request_id: str, plan_text: str):
        # FIX: persistent plan approval state machine
        # Persist the approval gate so a teammate cannot keep executing after
        # submitting a plan, and so /team can show what it is waiting on.
        member = self._find_member(name)
        if member:
            member["status"] = "waiting_approval"
            member["pending_plan_request_id"] = request_id
            member["pending_plan"] = plan_text
            self._save_config()

    def _clear_plan_waiting(self, name: str, status: str = "idle"):
        # FIX: persistent plan approval state machine
        # Approval/rejection clears the pending fields; the teammate returns to
        # idle until its loop resumes actual work.
        member = self._find_member(name)
        if member:
            member["status"] = status
            member.pop("pending_plan_request_id", None)
            member.pop("pending_plan", None)
            self._save_config()

    def _is_waiting_approval(self, name: str) -> bool:
        member = self._find_member(name)
        return bool(member and member.get("status") == "waiting_approval")

    def _find_pending_plan_request(self, request_id: str) -> dict | None:
        # FIX: Recover pending plan approvals from persisted teammate config after restart.
        for member in self.config.get("members", []):
            if member.get("pending_plan_request_id") == request_id:
                return {
                    "from": member.get("name", ""),
                    "plan": member.get("pending_plan", ""),
                    "status": "pending",
                }
        return None

    def _handle_plan_approval_response(self, name: str, msg: dict) -> str:
        # FIX: teammate consumes plan approval replies
        # Treat approval responses as protocol messages, not ordinary chat.
        member = self._find_member(name)
        if not member:
            return json.dumps(msg)
        expected = member.get("pending_plan_request_id")
        req_id = msg.get("request_id", "")
        if expected and req_id != expected:
            return (
                f"<plan_approval_ignored request_id=\"{req_id}\" "
                f"expected=\"{expected}\">Mismatched approval response.</plan_approval_ignored>"
            )
        approve = bool(msg.get("approve"))
        feedback = msg.get("feedback", "")
        next_action = msg.get("next_action") or ("proceed" if approve else "stop")
        self._clear_plan_waiting(name, "idle")
        self._set_initial_plan_required(name, next_action == "revise")
        if approve:
            return (
                f"<plan_approved request_id=\"{req_id}\" next_action=\"proceed\">"
                f"You may proceed with the approved plan.</plan_approved>"
            )
        if next_action == "revise":
            return (
                f"<plan_rejected request_id=\"{req_id}\" next_action=\"revise\">"
                f"Feedback: {feedback}\n"
                f"Revise your plan and submit a new plan_approval request before doing major work."
                f"</plan_rejected>"
            )
        return (
            f"<plan_rejected request_id=\"{req_id}\" next_action=\"stop\">"
            f"Feedback: {feedback}\n"
            f"Stop working on this task and return to idle. Do not submit another plan unless the lead asks you to."
            f"</plan_rejected>"
        )

    def _drain_protocol_inbox(self, name: str, messages: list) -> list:
        # FIX: teammate inbox protocol handling
        # Approval replies update the persisted gate before the LLM sees them.
        inbox = BUS.read_inbox(name)
        for msg in inbox:
            if msg.get("type") == "plan_approval_response":
                messages.append({
                    "role": "user",
                    "content": self._handle_plan_approval_response(name, msg),
                })
            else:
                messages.append({"role": "user", "content": json.dumps(msg)})
        return inbox


    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find_member(name)
        if member:
            existing = self.threads.get(name)
            if existing and existing.is_alive():
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()

        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )

        self.threads[name] = thread
        if self._prompt_requires_initial_plan(prompt):
            self._set_initial_plan_required(name, True)
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def wake(self, name: str, prompt: str = "") -> str:
        member = self._find_member(name)
        if not member:
            return f"Error: Unknown teammate '{name}'"
        if member.get("status") == "shutdown":
            return f"Error: '{name}' is shutdown. Spawn them again to restart."
        existing = self.threads.get(name)
        if existing and existing.is_alive():
            return f"'{name}' is already {member.get('status', 'working')}"
        wake_prompt = prompt or (
            "You are being resumed from idle. Read your inbox first, then handle "
            "any pending messages or available tasks."
        )
        return self.spawn(name, member.get("role", "teammate"), wake_prompt)



    def _teammate_loop(self, name: str, role: str, prompt: str):
        team_name = self.config["team_name"]
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            f"Use Windows shell commands such as dir, type, cd, and where. "
            f"Do not assume Unix commands like ls, head, cat, or pwd are available. "
            f"Use send_message only for normal chat. Complete your task. "
            f"If your prompt asks for a plan, call the plan_approval tool early, after at most a brief inspection. "
            f"Submit plans via the plan_approval tool before major work; do not send plan_approval_request with send_message. "
            f"If a plan is rejected with next_action='stop', stop the current task and enter idle. "
            f"If it is rejected with next_action='revise', revise and resubmit a plan before doing major work. "
            f"Respond to shutdown_request with shutdown_response. "
            f"Use idle tool when you have no more work. Idle is a standby state, not shutdown. You will auto-claim new tasks."
        )
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()
        profile = get_model_profile("weak")
        should_exit = False
        while True:
            for _ in range(50):
                # FIX: teammate inbox protocol handling
                # Approval responses are consumed by the state machine before
                # ordinary messages are appended to model context.
                self._drain_protocol_inbox(name, messages)
                if should_exit:
                    self._set_status(name, "shutdown")
                    return
                try:
                    response = profile.client.messages.create(
                        model=profile.model,system=sys_prompt,messages=messages,
                        tools=tools,max_tokens=8000,)
                except Exception:
                    break
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                idle_requested = False
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            idle_requested = True
                            output = "Entering idle phase. Will poll for new tasks."
                        else:
                            output = self._exec(name, block.name, block.input)
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output),
                        })
                        if block.name == "shutdown_response" and block.input.get("approve"):
                            should_exit = True
                        if block.name == "plan_approval" and self._is_waiting_approval(name):
                            # FIX: Stop the work loop immediately after submitting a plan.
                            idle_requested = True
                messages.append({"role": "user", "content": results})
                if should_exit:
                    self._set_status(name, "shutdown")
                    return
                if idle_requested:
                    break

            # -- IDLE PHASE: poll for inbox messages and unclaimed tasks --
            if not self._is_waiting_approval(name):
                self._set_status(name, "idle")
            resume = False
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            for _ in range(polls):
                time.sleep(POLL_INTERVAL)
                inbox = BUS.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "plan_approval_response":
                            # FIX: teammate consumes plan approval replies
                            messages.append({
                                "role": "user",
                                "content": self._handle_plan_approval_response(name, msg),
                            })
                            continue
                        if msg.get("type") == "shutdown_request":
                            # FIX: idle shutdown acknowledgement
                            # Even while idle, keep the shutdown protocol consistent:
                            # update the lead-side tracker and send a shutdown_response
                            # before the teammate exits.
                            req_id = msg.get("request_id", "")
                            if req_id:
                                with _tracker_lock:
                                    if req_id in shutdown_requests:
                                        shutdown_requests[req_id]["status"] = "approved"
                            BUS.send(
                                name,
                                "lead",
                                "Idle shutdown acknowledged.",
                                "shutdown_response",
                                {"request_id": req_id, "approve": True},
                            )
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                if self._is_waiting_approval(name):
                    # FIX: plan approval wait loop
                    # While approval is pending, do not auto-claim unrelated
                    # tasks and do not leave the waiting state.
                    continue
                unclaimed = scan_unclaimed_tasks()
                if unclaimed:
                    task = unclaimed[0]
                    result = claim_task(task["id"], name)
                    if result.startswith("Error:"):
                        continue
                    task_prompt = (
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    if len(messages) <= 3:
                        messages.insert(0, make_identity_block(name, role, team_name))
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                    messages.append({"role": "user", "content": task_prompt})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break
            if not resume:
                if self._is_waiting_approval(name):
                    # FIX: plan approval wait loop
                    # Keep polling for lead approval instead of timing out to
                    # shutdown while the persisted gate is still active.
                    continue
                self._set_status(name, "idle")
                continue
            self._set_status(name, "working")


    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        initial_plan_error = self._check_initial_plan_gate(sender, tool_name)
        if initial_plan_error:
            return initial_plan_error

        safe_while_waiting = {"read_inbox", "send_message", "idle", "task_get"}
        if self._is_waiting_approval(sender) and tool_name not in safe_while_waiting:
            # FIX: plan approval execution gate
            # Once a plan is submitted, block mutating/active tools until the
            # lead's plan_approval_response clears the persisted gate.
            member = self._find_member(sender) or {}
            req_id = member.get("pending_plan_request_id", "")
            return (
                "Error: waiting for plan approval"
                f"{f' (request_id={req_id})' if req_id else ''}; "
                "only read_inbox, send_message, idle, and task_get are allowed."
            )
        if tool_name == "bash":
            return _run_bash(args["command"])
        if tool_name == "read_file":
            return _run_read(args["path"])
        if tool_name == "write_file":
            return _run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            msg_type = args.get("msg_type", "message")
            if msg_type != "message":
                return (
                    f"Error: teammates may not send protocol message type '{msg_type}' "
                    "through send_message. Use the dedicated protocol tool instead."
                )
            return BUS.send(
                sender,
                args["to"],
                args["content"],
                "message"
            )
        if tool_name == "read_inbox":
            inbox = BUS.read_inbox(sender)
            processed = []
            for msg in inbox:
                if msg.get("type") == "plan_approval_response":
                    processed.append(self._handle_plan_approval_response(sender, msg))
                else:
                    processed.append(msg)
            return json.dumps(processed, indent=2, ensure_ascii=False)
        if tool_name == "task_get":
            return TASKS.get(args["task_id"])
        if tool_name == "shutdown_response":
            req_id = args["request_id"]
            approve = args["approve"]

            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if approve else "rejected"

            BUS.send(
                sender, "lead", 
                args.get("reason", ""),
                "shutdown_response",
                {"request_id": req_id, "approve": approve},
            )
            return f"Shutdown {'approved' if approve else 'rejected'}"
        if tool_name == "plan_approval":
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]

            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            self._set_plan_waiting(sender, req_id, plan_text)
            self._set_initial_plan_required(sender, False)

            BUS.send(
                sender, "lead", 
                plan_text,
                "plan_approval_request",     # FIX: clearer plan approval message naming
                {"request_id": req_id, "plan": plan_text},
            )
            return f"Plan submitted (request_id={req_id}). Waiting for lead approval."
        if tool_name == "claim_task":
            return claim_task(args["task_id"], sender)
        return f"Unknown tool: {tool_name}"


    def _teammate_tools(self) -> list:
        return [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},

            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},

            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},

            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},

            {"name": "send_message", "description": "Send a normal chat message to a teammate. Use plan_approval or shutdown_response for protocol messages.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}}, "required": ["to", "content"]}},

            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},

            {"name": "idle", "description": "Enter idle polling while waiting for messages or tasks.",
             "input_schema": {"type": "object", "properties": {}}},

            {"name": "task_get", "description": "Read-only: get full details of a task by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},

            {"name": "shutdown_response", "description": "Respond to a shutdown request. Approve to shut down, reject to keep working.",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},

            {"name": "plan_approval", "description": "Submit a plan for lead approval. Provide plan text.",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},

            {"name": "claim_task", "description": "Claim a task from the task board by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            pending = ""
            if m.get("pending_plan_request_id"):
                # FIX: visible persistent plan approval state
                pending = f" waiting_for_plan={m['pending_plan_request_id']}"
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}{pending}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


# 全局团队管理器实例
TEAM = TeammateManager(TEAM_DIR)

