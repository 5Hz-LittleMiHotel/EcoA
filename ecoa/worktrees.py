import json
import re
import subprocess
import time
from pathlib import Path

from .config import REPO_ROOT
from .events import EVENTS, EventBus
from .file_tools import redact_secrets
from .task_board import TASKS, TaskManager

# %% -- WorktreeManager: create/list/run/remove git worktrees + lifecycle index --
class WorktreeManager:
    def __init__(self, repo_root: Path, tasks: TaskManager, events: EventBus):
        self.repo_root = repo_root
        self.tasks = tasks
        self.events = events
        self.dir = repo_root / ".worktrees"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        if not self.index_path.exists():
            self.index_path.write_text(json.dumps({"worktrees": []}, indent=2))
        self.git_available = self._is_git_repo()

    def _is_git_repo(self) -> bool:
        """私有方法：检查是否在 Git 仓库中"""
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _run_git(self, args: list[str]) -> str:
        """私有方法：在仓库根目录运行 Git 命令"""
        if not self.git_available:
            raise RuntimeError("Not in a git repository. worktree tools require git.")
        r = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=120,
        )
        if r.returncode != 0:
            msg = redact_secrets((r.stdout + r.stderr).strip())
            raise RuntimeError(msg or f"git {' '.join(args)} failed")
        return redact_secrets((r.stdout + r.stderr).strip()) or "(no output)"

    def _load_index(self) -> dict:
        return json.loads(self.index_path.read_text())

    def _save_index(self, data: dict):
        self.index_path.write_text(json.dumps(data, indent=2))

    def _find(self, name: str) -> dict | None:
        idx = self._load_index() # 先加载索引
        # 遍历索引中的 worktrees 列表
        for wt in idx.get("worktrees", []):
            if wt.get("name") == name: # 找到匹配名称的条目
                return wt # 返回该条目
        return None # 未找到返回 None

    def _validate_name(self, name: str):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):
            raise ValueError(
                "Invalid worktree name. Use 1-40 chars: letters, numbers, ., _, -"
            )

    def create(self, name: str, task_id: int = None, base_ref: str = "HEAD") -> str:
        self._validate_name(name) # 1. 校验名称
        # 2. 检查索引中是否已存在同名 worktree
        if self._find(name):
            raise ValueError(f"Worktree '{name}' already exists in index")
        # 3. 检查任务 ID 是否存在（如果提供了 task_id）
        if task_id is not None and not self.tasks.exists(task_id):
            raise ValueError(f"Task {task_id} not found")
        # 4. 定义路径和分支名
        path = self.dir / name 
        branch = f"wt/{name}" 
        # 5. 发布事件：创建开始
        self.events.emit(
            "worktree.create.before",
            task={"id": task_id} if task_id is not None else {},
            worktree={"name": name, "base_ref": base_ref},
        )
        try:
            # 6. 执行 Git 命令创建
            self._run_git(["worktree", "add", "-b", branch, str(path), base_ref])
            # 7. 构造索引条目
            entry = {
                "name": name,
                "path": str(path),
                "branch": branch, 
                "task_id": task_id,
                "status": "active", 
                "created_at": time.time(), 
            }
            # 8. 更新索引文件
            idx = self._load_index()
            idx["worktrees"].append(entry) # 添加新条目
            self._save_index(idx) # 保存文件
            # 9. 如果绑定了任务，更新任务状态为 in_progress
            if task_id is not None:
                self.tasks.bind_worktree(task_id, name)
            # 10. 发布事件：创建成功
            self.events.emit(
                "worktree.create.after",
                task={"id": task_id} if task_id is not None else {},
                worktree={ "name": name, "path": str(path), "branch": branch, "status": "active" },
            )
            # 11. 返回创建的条目信息（JSON 字符串）
            return json.dumps(entry, indent=2)
        except Exception as e:
            # 12. 异常处理：记录失败事件并重新抛出异常
            self.events.emit(
                "worktree.create.failed",
                task={"id": task_id} if task_id is not None else {},
                worktree={"name": name, "base_ref": base_ref},
                error=str(e),
            )
            raise

    def list_all(self) -> str:
        """公开方法：列出所有受管理的 worktree"""
        idx = self._load_index()
        wts = idx.get("worktrees", [])
        if not wts:
            return "No worktrees in index."

        lines = []
        for wt in wts:
            suffix = f" task={wt['task_id']}" if wt.get("task_id") else ""
            lines.append(
                f"[{wt.get('status', 'unknown')}] {wt['name']} -> "
                f"{wt['path']} ({wt.get('branch', '-')}){suffix}"
            )
        return "\n".join(lines)

    def status(self, name: str) -> str:
        """公开方法：查看指定 worktree 的 Git 状态"""
        wt = self._find(name) # 1. 查找
        if not wt:
            return f"Error: Unknown worktree '{name}'"

        path = Path(wt["path"])
        if not path.exists():
            return f"Error: Worktree path missing: {path}"

        # 2. 在 worktree 目录下执行 git status
        r = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=path,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
        text = redact_secrets((r.stdout + r.stderr).strip())
        return text or "Clean worktree"

    def run(self, name: str, command: str) -> str:
        """公开方法：在指定 worktree 目录中运行 Shell 命令"""
        # 1. 安全检查：拦截危险命令（rm -rf / 等）
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            return "Error: Dangerous command blocked"
        wt = self._find(name) # 2. 查找 worktree
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        path = Path(wt["path"])
        if not path.exists():
            return f"Error: Worktree path missing: {path}"
        try:
            r = subprocess.run(
                command,
                shell=True,
                cwd=path,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=300, # 5分钟超时
            )
            out = redact_secrets((r.stdout + r.stderr).strip())
            # 4. 截断过长的输出，防止 Token 爆炸
            return out[:50000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: Timeout (300s)"


    def remove(self, name: str, force: bool = False, complete_task: bool = False) -> str:
        """公开方法：移除 worktree（物理删除目录）"""
        wt = self._find(name) # 1. 查找
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        # 2. 发布事件：移除开始
        self.events.emit(
            "worktree.remove.before",
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
            worktree={"name": name, "path": wt.get("path")},
        )
        try:
            # 3. 构造 git worktree remove 命令参数
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(wt["path"])
            self._run_git(args)
            # 4. 如果设置了 complete_task，同时更新关联的任务状态
            if complete_task and wt.get("task_id") is not None:
                task_id = wt["task_id"]
                before = json.loads(self.tasks.get(task_id))
                self.tasks.update(task_id, status="completed")
                self.tasks.unbind_worktree(task_id)
                # 发布任务完成事件
                self.events.emit(
                    "task.completed",
                    task={ "id": task_id, "subject": before.get("subject", ""), "status": "completed" },
                    worktree={"name": name},
                )
            # 5. 更新本地索引文件，标记状态为 removed
            idx = self._load_index()
            for item in idx.get("worktrees", []):
                if item.get("name") == name:
                    item["status"] = "removed"
                    item["removed_at"] = time.time()
            self._save_index(idx)
            # 6. 发布事件：移除成功
            self.events.emit(
                "worktree.remove.after",
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
                worktree={"name": name, "path": wt.get("path"), "status": "removed"},
            )
            return f"Removed worktree '{name}'"
        except Exception as e:
            # 7. 异常处理
            self.events.emit(
                "worktree.remove.failed",
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
                worktree={"name": name, "path": wt.get("path")},
                error=str(e),
            )
            raise


    def keep(self, name: str) -> str:
        """公开方法：标记 worktree 为保留状态（不自动删除）"""
        wt = self._find(name) # 1. 查找
        if not wt:
            return f"Error: Unknown worktree '{name}'"

        # 2. 更新索引中的状态为 'kept'
        idx = self._load_index()
        kept = None
        for item in idx.get("worktrees", []):
            if item.get("name") == name:
                item["status"] = "kept"
                item["kept_at"] = time.time()
                kept = item
        self._save_index(idx)

        # 3. 发布保留事件
        self.events.emit(
            "worktree.keep",
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
            worktree={ "name": name, "path": wt.get("path"), "status": "kept" },
        )

        # 4. 返回结果
        return json.dumps(kept, indent=2) if kept else f"Error: Unknown worktree '{name}'"

WORKTREES = WorktreeManager(REPO_ROOT, TASKS, EVENTS)

