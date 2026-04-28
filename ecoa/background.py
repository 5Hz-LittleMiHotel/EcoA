import subprocess
import threading
import uuid

from .config import WORKDIR
from .file_tools import redact_secrets

# %% -- BackgroundManager: threaded execution + notification queue --
# 后台任务管理器：用于在后台线程执行命令，并通过队列返回完成通知
class BackgroundManager:
    def __init__(self):
        # 存储所有任务：task_id -> {status, result, command}
        self.tasks = {}
        # 用于存储已完成任务的通知队列（供外部轮询获取）
        self._notification_queue = []
        # 线程锁，保证多线程访问队列时安全
        self._lock = threading.Lock()


    def run(self, command: str) -> str:
        """Start a background thread, return task_id immediately. 
           启动后台线程，返回task_id"""
        # 生成一个短任务ID（UUID前8位）
        task_id = str(uuid.uuid4())[:8]
        # 初始化任务状态为 running
        self.tasks[task_id] = {
            "status": "running",
            "result": None,
            "command": command
        }
        # 创建后台线程执行命令
        thread = threading.Thread(
            target=self._execute,          # 线程执行函数
            args=(task_id, command),       # 传入任务ID和命令
            daemon=True                   # 守护线程（主程序退出时自动结束）
        )
        # 启动线程
        thread.start()
        # 立即返回任务启动信息（不会阻塞）
        return f"Background task {task_id} started: {command[:80]}"


    def _execute(self, task_id: str, command: str):
        """Thread target: run subprocess, capture output, push to queue.
           线程目标:运行子流程，捕获输出，推入队列。"""
        try:
            # 执行系统命令
            r = subprocess.run(
                command,
                shell=True,              # 使用shell执行（支持字符串命令）
                cwd=WORKDIR,             # 指定工作目录
                capture_output=True,     # 捕获stdout和stderr
                text=True,               # 输出以字符串形式返回
                errors="replace",
                timeout=300              # 超时时间300秒
            )

            # 拼接标准输出和错误输出，并限制最大长度
            output = redact_secrets((r.stdout + r.stderr).strip())[:50000]

            # 标记任务完成
            status = "completed"

        except subprocess.TimeoutExpired:
            # 超时异常处理
            output = "Error: Timeout (300s)"
            status = "timeout"
        except Exception as e:
            # 其他异常处理
            output = f"Error: {e}"
            status = "error"

        # 更新任务状态和结果
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(no output)"

        # 将任务完成信息加入通知队列（线程安全）
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],              # 截断命令用于展示
                "result": (output or "(no output)")[:500],  # 截断结果用于通知
            })

    def check(self, task_id: str = None) -> str:
        """Check status of one task or list all."""
        if task_id:
            # 查询指定任务
            t = self.tasks.get(task_id)

            # 如果任务不存在
            if not t:
                return f"Error: Unknown task {task_id}"

            # 返回任务状态和结果（如果还在运行则显示running）
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"

        # 如果未指定task_id，则列出所有任务
        lines = []
        for tid, t in self.tasks.items():
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")

        # 返回所有任务列表
        return "\n".join(lines) if lines else "No background tasks."

    def drain_notifications(self) -> list:
        """Return and clear all pending completion notifications.
           在LLM调用之前，清空后台通知并作为系统消息注入"""
        # 加锁确保线程安全
        with self._lock:
            # 拷贝当前通知队列
            notifs = list(self._notification_queue)
            # 清空队列（避免重复读取）
            self._notification_queue.clear()

        # 返回通知列表
        return notifs


# 创建一个全局后台任务管理器实例
BG = BackgroundManager()

