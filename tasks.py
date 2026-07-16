"""后台任务管理：每类任务（scrape / download / login）同时只跑一个，支持取消。"""
import threading


class Task:
    def __init__(self, name):
        self.name = name
        self.running = False
        self.message = ""
        self.progress = {}      # 任务自定义的进度数据
        self.error = None
        self.cancel_event = threading.Event()

    def to_dict(self):
        return {"running": self.running, "message": self.message,
                "progress": self.progress, "error": self.error,
                "cancelled": self.cancel_event.is_set()}


class TaskManager:
    def __init__(self):
        self._tasks = {}
        self._lock = threading.Lock()

    def get(self, name):
        with self._lock:
            if name not in self._tasks:
                self._tasks[name] = Task(name)
            return self._tasks[name]

    def start(self, name, target):
        """启动任务。target(task) 在后台线程执行。已在运行则返回 False。"""
        with self._lock:
            task = self._tasks.setdefault(name, Task(name))
            if task.running:
                return False
            task.running = True
            task.message = "启动中…"
            task.error = None
            task.progress = {}
            task.cancel_event.clear()

        def worker():
            try:
                target(task)
            except Exception as e:
                task.error = "%s" % e
                task.message = "任务出错"
            finally:
                task.running = False

        threading.Thread(target=worker, daemon=True).start()
        return True

    def cancel(self, name):
        task = self.get(name)
        if task.running:
            task.cancel_event.set()
            task.message = "正在取消…"
            return True
        return False

    def status(self):
        with self._lock:
            return {name: t.to_dict() for name, t in self._tasks.items()}


manager = TaskManager()
