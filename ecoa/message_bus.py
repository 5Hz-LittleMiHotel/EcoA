import json
import threading
import time
from pathlib import Path

from .config import INBOX_DIR
from .protocols import VALID_MSG_TYPES

# -- MessageBus: JSONL inbox per teammate --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self._inbox_lock = threading.Lock()
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with self._inbox_lock:
            with open(inbox_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"Sent {msg_type} to {to}"


    def read_inbox(self, name: str) -> list:        
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        with self._inbox_lock:
            messages = []
            for line in inbox_path.read_text(encoding="utf-8").strip().splitlines():
                if line:
                    messages.append(json.loads(line))
            inbox_path.write_text("", encoding="utf-8")
        return messages

    def requeue_inbox(self, name: str, messages: list):
        if not messages:
            return
        inbox_path = self.dir / f"{name}.jsonl"
        with self._inbox_lock:
            existing = inbox_path.read_text(encoding="utf-8") if inbox_path.exists() else ""
            restored = "\n".join(json.dumps(msg, ensure_ascii=False) for msg in messages)
            content = restored + "\n"
            if existing:
                content += existing
            inbox_path.write_text(content, encoding="utf-8")


    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                # 调用send发送广播消息
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


BUS = MessageBus(INBOX_DIR)

