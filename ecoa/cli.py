import json

from .agent import agent_loop
from .config import REPO_ROOT
from .message_bus import BUS
from .task_board import TASKS
from .teammates import TEAM
from .worktrees import WORKTREES


def _print_latest_response(history: list):
    response_content = history[-1]["content"]
    if isinstance(response_content, str):
        print()
        print("----------------------this is the response----------------------")
        print(response_content)
    if isinstance(response_content, list):
        for block in response_content:
            if hasattr(block, "text"):
                print()
                print("----------------------this is the response----------------------")
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print()
                print("----------------------this is the response----------------------")
                print(block.get("text", ""))
    print()


def _process_lead_inbox(history: list) -> bool:
    inbox = BUS.read_inbox("lead")
    if not inbox:
        return False

    history.append({
        "role": "user",
        "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
    })
    try:
        agent_loop(history)
    except Exception as exc:
        history.pop()
        BUS.requeue_inbox("lead", inbox)
        print(f"Error processing lead inbox: {exc}")
        return True
    _print_latest_response(history)
    return True


def main() -> None:
    print(f"Repo root for s12: {REPO_ROOT}")
    if not WORKTREES.git_available:
        print("Note: Not in a git repo. worktree_* tools will return errors.")
    print("Press Enter to process pending lead inbox messages; use q or exit to quit.")

    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break

        command = query.strip()
        if command.lower() in ("q", "exit"):
            break
        if command == "":
            if not _process_lead_inbox(history):
                print("(no pending lead inbox messages)")
            continue
        if command == "/team":
            print(TEAM.list_all())
            continue
        if command == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        if command == "/tasks":
            print(TASKS.list_all())
            continue

        history.append({"role": "user", "content": query})
        agent_loop(history)
        _print_latest_response(history)


if __name__ == "__main__":
    main()
