import re
import subprocess
from pathlib import Path

from .config import WORKDIR

# %% ------------ Tool implementations(sandbox) ------------
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*=\s*([^\s]+)"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
]


def redact_secrets(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}=<redacted>", text)
        else:
            text = pattern.sub("<redacted-secret>", text)
    return text


def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR): 
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def is_protected_path(path: Path) -> bool:
    name = path.name.lower()
    return name == ".env" or name.startswith(".env.")

def _run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR, 
                           capture_output=True,
                           text=True, errors="replace", timeout=120)
        out = redact_secrets((r.stdout + r.stderr).strip())
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"



# %% ---------------- Tool implementations ----------------

def _run_read(path: str, limit: int = None) -> str:
    try:
        fp = safe_path(path)
        if is_protected_path(fp):
            return f"Error: Refusing to read protected file: {path}"
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        if limit and limit < len(lines): 
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return redact_secrets("\n".join(lines))[:50000]
    except Exception as e:
        return f"Error: {e}"


def _run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        if is_protected_path(fp):
            return f"Error: Refusing to write protected file: {path}"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        if is_protected_path(fp):
            return f"Error: Refusing to edit protected file: {path}"
        content = fp.read_text(encoding="utf-8", errors="replace")
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

