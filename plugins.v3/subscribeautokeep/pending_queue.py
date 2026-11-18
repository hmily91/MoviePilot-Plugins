"""
订阅自动保种助手 - 延迟等待队列。

命中全部规则后不立即删除，先进入等待队列。等待期间由定时任务持续复检：
    - 打包种子是否仍能在勾选站点检索到；
    - 打包种子是否仍然满足同季整包校验；
    - 「新全集已就绪」门槛（可选）是否满足。
任一条件在等待期内失效即取消任务，避免刚发布不稳定资源误触发清理。

队列状态通过宿主基类的 save_data / get_data 持久化，天然支持虚拟分身隔离，
本模块本身不访问数据库，只负责纯状态结构与序列化，便于单测。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

#: 等待队列状态在插件数据区的键名
QUEUE_DATA_KEY = "subscribeautokeep_pending_queue"


def make_task_id(subscribe_id: int, site_id: Optional[int], torrent_title: str) -> str:
    """同一订阅 + 同一站点 + 同一打包种子视为同一个等待任务，避免重复入队。"""
    fingerprint = f"{subscribe_id}|{site_id}|{torrent_title}".encode("utf-8", "ignore")
    return hashlib.sha1(fingerprint).hexdigest()[:16]


@dataclass
class PendingTask:
    """一条待执行的延迟清理任务。"""

    task_id: str
    subscribe_id: int
    subscribe_name: str
    season: Optional[int]
    media_source: str = ""
    media_id: str = ""
    site_id: Optional[int] = None
    site_name: str = ""
    torrent_title: str = ""
    torrent_page_url: str = ""
    torrent_enclosure: str = ""
    resolutions: list[str] = field(default_factory=list)
    matched_include: list[str] = field(default_factory=list)
    verdict_detail: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    created_ts: float = 0.0
    execute_ts: float = 0.0
    last_check_ts: float = 0.0
    check_count: int = 0
    consecutive_missing: int = 0
    ready_confirmed: bool = False
    status: str = "waiting"  # waiting / executing / done / cancelled
    last_reason: str = ""

    @property
    def remaining_seconds(self) -> float:
        """距离执行时刻的剩余秒数。"""
        return max(0.0, self.execute_ts - time.time())

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingTask":
        """从持久化字典恢复，忽略新增字段带来的旧数据差异。"""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class PendingQueue:
    """等待队列容器：去重、到期、取消、序列化。"""

    def __init__(self, tasks: Optional[list[PendingTask]] = None) -> None:
        self._tasks: dict[str, PendingTask] = {t.task_id: t for t in (tasks or [])}

    @classmethod
    def from_list(cls, raw: Any) -> "PendingQueue":
        """从 save_data 读出的列表恢复队列；脏数据会被安全忽略。"""
        if not isinstance(raw, list):
            return cls()
        tasks: list[PendingTask] = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("task_id"):
                continue
            try:
                tasks.append(PendingTask.from_dict(item))
            except TypeError:
                continue
        return cls(tasks)

    def to_list(self) -> list[dict[str, Any]]:
        """转成可持久化列表。"""
        return [task.to_dict() for task in self._tasks.values()]

    def all(self) -> list[PendingTask]:
        """返回全部任务（含已结束的，供详情页展示）。"""
        return list(self._tasks.values())

    def waiting(self) -> list[PendingTask]:
        """返回仍在等待中的任务。"""
        return [t for t in self._tasks.values() if t.status == "waiting"]

    def get(self, task_id: str) -> Optional[PendingTask]:
        """按 ID 取任务。"""
        return self._tasks.get(task_id)

    def upsert(self, task: PendingTask) -> bool:
        """
        入队或刷新已存在的同一任务。

        :return: True 表示新建，False 表示刷新了已有任务的复核时间。
        """
        existing = self._tasks.get(task.task_id)
        if existing and existing.status == "waiting":
            existing.last_check_ts = time.time()
            existing.check_count += 1
            existing.consecutive_missing = 0
            existing.torrent_title = task.torrent_title
            existing.torrent_page_url = task.torrent_page_url
            existing.torrent_enclosure = task.torrent_enclosure
            existing.resolutions = task.resolutions
            existing.matched_include = task.matched_include
            existing.verdict_detail = task.verdict_detail
            return False
        self._tasks[task.task_id] = task
        return True

    def mark(self, task_id: str, status: str, reason: str = "") -> Optional[PendingTask]:
        """更新任务状态与原因。"""
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.status = status
        task.last_reason = reason
        task.last_check_ts = time.time()
        return task

    def remove(self, task_id: str) -> None:
        """从队列中移除任务。"""
        self._tasks.pop(task_id, None)

    def purge_terminal(self, keep: int = 30) -> int:
        """仅保留最近 keep 条已结束任务，避免状态无限膨胀。"""
        terminal = sorted(
            (t for t in self._tasks.values() if t.status in {"done", "cancelled"}),
            key=lambda t: t.last_check_ts or t.created_ts,
            reverse=True,
        )
        removed = 0
        for task in terminal[keep:]:
            self._tasks.pop(task.task_id, None)
            removed += 1
        return removed

    def due(self, now: Optional[float] = None) -> list[PendingTask]:
        """返回已到执行时刻、且仍处于等待中的任务。"""
        current = now if now is not None else time.time()
        return [t for t in self.waiting() if t.execute_ts <= current]

    def clear(self) -> int:
        """清空队列，返回被清除的任务数。"""
        count = len(self._tasks)
        self._tasks.clear()
        return count
