"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

本模块额外提供两件分群/标签链路需要的能力：
- ``lock``：进程内互斥锁。刷新分群与回写标签覆盖对象数必须在同一临界区内完成，
  并发刷新同一分群时由业务层在此锁内做幂等判断，保证首份完整结果胜出、失败不留半成品。
- ``transaction``：基于深拷贝快照的事务上下文。计算成功后同时改分群最终规模和标签
  覆盖对象数，任一步失败整体回滚，杜绝单侧改写。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """分群/标签写链路共用的互斥锁，可重入以便事务嵌套调用。"""
        return self._lock

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """事务：进入时拍快照，块内异常时把所有表整体恢复，保证多表写要么全成要么全不成。"""
        with self._lock:
            snapshot = copy.deepcopy(self._tables)
            try:
                yield
            except Exception:
                self._tables = snapshot
                raise

    def reset(self) -> None:
        """测试辅助：恢复到种子数据，避免用例之间互相污染。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_code(self, module: str, code_field: str, code: str) -> dict[str, Any] | None:
        """按业务编号（如标签编号 TAGM-NIGHT）定位记录，供分群引用解析使用。"""
        for row in self.rows(module):
            if str(row.get(code_field) or "") == code:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
