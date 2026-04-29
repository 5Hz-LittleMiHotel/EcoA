import json

from .background import BG
from .compression import auto_compact, estimate_tokens, micro_compact
from .config import THRESHOLD, get_model_profile
from .message_bus import BUS
from .prompts import DIRECT_EXECUTOR_SYSTEM
from .todo import TODO
from .tool_registry import EXECUTOR_TOOL_HANDLERS, EXECUTOR_TOOLS

# %% ---------------- Agent loop with nag reminder injection ----------------

MAX_TOOL_ROUNDS = 40
EMPTY_INBOX_STALL_THRESHOLD = 6


def _append_local_response(messages: list, text: str):
    messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})


def latest_assistant_text(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
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
            return "\n".join(part for part in parts if part).strip()
    return ""


def run_react_loop(
    messages: list,
    model_profile=None,
    system_prompt: str | None = None,
    tools: list | None = None,
    tool_handlers: dict | None = None,
    max_tokens: int = 8000,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
    drain_background: bool = True,
    drain_lead_inbox: bool = False,
    compact_messages: bool = True,
):
    profile = get_model_profile(model_profile)
    active_tools = EXECUTOR_TOOLS if tools is None else tools
    active_handlers = EXECUTOR_TOOL_HANDLERS if tool_handlers is None else tool_handlers
    active_system = system_prompt or DIRECT_EXECUTOR_SYSTEM
    rounds_since_todo = 0
    tool_rounds = 0
    empty_inbox_reads = 0
    while True:
        tool_rounds += 1
        if tool_rounds > max_tool_rounds:
            _append_local_response(
                messages,
                "Stopped after repeated tool calls without progress. "
                "The current ReAct pass may be blocked; ask the orchestrator for a planned run if needed.",
            )
            return

        if compact_messages:
            micro_compact(messages)
        if compact_messages and estimate_tokens(messages) > THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = auto_compact(messages)

        if drain_background:
            notifs = BG.drain_notifications()
            if notifs and messages:
                notif_text = "\n".join(
                    f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
                )
                messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})

        if drain_lead_inbox:
            inbox = BUS.read_inbox("lead")
            if inbox:
                messages.append({
                    "role": "user",
                    "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
                })
        response = profile.client.messages.create(
            model=profile.model, system=active_system, messages=messages,
            tools=active_tools, max_tokens=max_tokens,
        )
        messages.append({"role": "assistant","content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        used_todo =False 
        manual_compact = False
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    manual_compact = True
                    output = "Compressing..."
                else:

                    handler = active_handlers.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"

                # 打印命令与部分输出
                print(f"\n> {block.name}:")
                print(str(output)[:200])
                # 收集结果
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,  # 对应 tool_use 的 ID
                    "content": output
                })

                if block.name == "read_inbox" and str(output).strip() == "[]":
                    empty_inbox_reads += 1
                elif block.name == "read_inbox":
                    empty_inbox_reads = 0

                if block.name == "todo":
                    used_todo = True
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        # FIX: remind only after todo workflow exists and still has unfinished items
        if TODO.has_open_items() and rounds_since_todo >= 3:
            results.append({"type": "text", "text": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        if empty_inbox_reads >= EMPTY_INBOX_STALL_THRESHOLD:
            results.append({
                "type": "text",
                "text": (
                    "<coordination_stall>"
                    "The lead inbox has been empty after repeated checks. "
                    "Stop polling now and tell the user that the teammate has not submitted the expected message/plan yet. "
                    "Suggest checking /team or sending a direct reminder."
                    "</coordination_stall>"
                ),
            })
            empty_inbox_reads = 0

        messages.append({"role": "user","content": results})

        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages)


def agent_loop(messages: list):
    return run_react_loop(
        messages,
        model_profile="weak",
        system_prompt=DIRECT_EXECUTOR_SYSTEM,
        tools=EXECUTOR_TOOLS,
        tool_handlers=EXECUTOR_TOOL_HANDLERS,
        drain_lead_inbox=False,
    )
