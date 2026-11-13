"""审计台账：分群生效链路每一次关键动作都落一条不可变记录。

审计不依赖数据库，落在内存仓库的 ``audit`` 表里，与业务数据共享同一把锁、同一个事务，
保证“分群最终规模、标签覆盖对象数、审计记录”三者同时成功或同时回滚。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "audit"


class AuditService:
    def _next_id(self) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1

    def record(
        self,
        *,
        module: str,
        entry_id: int,
        code: str,
        action: str,
        ok: bool,
        detail: str,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加一条审计记录。正常情况下应在 store 事务内调用。"""
        entry = {
            "id": self._next_id(),
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "业务模块": module,
            "业务编号": code,
            "记录ID": entry_id,
            "动作": action,
            "结果": "成功" if ok else "拒绝",
            "说明": detail,
            "数据快照": dict(snapshot or {}),
        }
        store.rows(MODULE).append(entry)
        return entry

    def list_for(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        """读取某条业务记录的全部审计，按时间正序，方便从标签定义一路追到分群规模。"""
        return [
            dict(row)
            for row in store.rows(MODULE)
            if row.get("业务模块") == module and int(row.get("记录ID", 0)) == entry_id
        ]

    def list_all(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        return [dict(row) for row in rows[-limit:][::-1]]


audit_service = AuditService()
