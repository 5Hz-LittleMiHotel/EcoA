import json
import time

from .config import KEEP_RECENT, TRANSCRIPT_DIR, get_model_profile
from .todo import TODO

# %% ------------------------- compression -------------------------

def estimate_tokens(messages: list) -> int:
    return len(str(messages)) // 4


def extract_critical(messages):
    errors = []
    for msg in messages:
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    content = part.get("content", "")
                    if "Error" in content or "Traceback" in content:
                        errors.append(content[-2000:])  # 只保留尾部
    return errors[-1] if errors else None


# -- Layer 1: micro_compact - replace old tool results with placeholders --
def micro_compact(messages: list) -> list:
    tool_results = []
    for msg_idx, msg in enumerate(messages):

        if msg["role"] == "user" and isinstance(msg.get("content"), list):

            for part_idx, part in enumerate(msg["content"]):

                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))

    if len(tool_results) <= KEEP_RECENT:
        return messages

    tool_name_map = {}

    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name

    to_clear = tool_results[:-KEEP_RECENT]

    for _, _, result in to_clear:
        if isinstance(result.get("content"), str) and len(result["content"]) > 100:
            tool_id = result.get("tool_use_id", "")
            tool_name = tool_name_map.get(tool_id, "unknown")
            result["content"] = f"[Previous: used {tool_name}]"

    return messages


# -- Layer 2: auto_compact - save transcript, summarize, replace messages --
def auto_compact(messages: list) -> list:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"

    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")

    print(f"[transcript saved: {transcript_path}]")

    conversation_text = json.dumps(messages, default=str)[:80000]
    profile = get_model_profile("strong")
    response = profile.client.messages.create(
        model=profile.model,
        messages=[{
            "role": "user",
            "content":
                "Summarize this conversation for continuity. Include: "
                "1) What was accomplished, 2) Current state, 3) Key decisions made. "
                "Be concise but preserve critical details.\n\n"
                + conversation_text
        }],
        max_tokens=2000,
    )

    summary = next((block.text for block in response.content if hasattr(block, "text")), "")
    if not summary:
        summary = "No summary generated."

    critical = extract_critical(messages)
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
            "content": "Understood. I have the context from the summary. Continuing."
        },
    ]

