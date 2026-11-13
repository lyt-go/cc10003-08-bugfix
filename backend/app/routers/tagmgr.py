"""标签管理接口：维护标签定义与版本，覆盖规则编辑、评审、发布、下线与换版追溯。

业务判断全部在 TagmgrService 内完成，路由层只负责把拒绝原因透传给前端。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.tagmgr import TagmgrService

router = APIRouter(prefix="/api/tagmgr", tags=["标签管理"])

service = TagmgrService()

LIST_FIELDS = ["标签编号", "标签名称", "标签分类", "计算逻辑", "值类型", "刷新周期",
               "规则集合", "当前版本", "覆盖对象数", "最近计算时间", "status"]
STATUSES = ["草稿", "逻辑评审中", "已发布", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按标签编号检索"),
    status: str | None = Query(default=None, description="草稿、逻辑评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按标签编号与状态过滤标签管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条标签定义明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"标签定义 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条标签定义，缺字段或编号重复时说明原因而不是静默丢弃。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="标签定义已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改标签定义与规则集合；已发布标签被修改会退回草稿，作为待发布的新版本。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条标签定义执行提交逻辑评审、发布标签、下线标签；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/trace")
def trace_entry(entry_id: int) -> dict[str, Any]:
    """追溯视图：标签定义 -> 版本快照 -> 引用分群的最终规模 -> 审计台账。"""
    result = service.trace(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"标签定义 {entry_id} 不存在或已归档")
    return result


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出标签管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "tagmgr", "total": total, "items": items}
