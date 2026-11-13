"""分群分析接口：维护人群分群，覆盖规则编辑、提交校验、刷新计算、生效/失效与审计追溯。

业务判断全部在 SegmentService 内完成，路由层只负责把拒绝原因透传给前端。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.segment import SegmentService

router = APIRouter(prefix="/api/segment", tags=["分群分析"])

service = SegmentService()

LIST_FIELDS = ["分群编号", "分群名称", "筛选规则", "关联标签编号", "标签版本", "最小规模", "最大规模",
               "刷新周期", "最近计算时间", "最终规模", "刷新状态", "校验原因"]
STATUSES = ["草稿", "规则校验中", "已生效", "已失效"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按分群编号检索"),
    status: str | None = Query(default=None, description="草稿、规则校验中、已生效、已失效"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按分群编号与状态过滤分群分析列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条人群分群明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"人群分群 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条人群分群，缺字段或规模上下限写反时说明原因而不是静默丢弃。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="人群分群已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改草稿/已失效分群的规则与规模；已生效或校验中的分群拒绝编辑。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条人群分群执行提交规则校验、生效分群、失效分群；不允许的流转会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/refresh", response_model=ActionResult)
def create_refresh(entry_id: int) -> ActionResult:
    """发起覆盖计算；并发刷新同一分群时首份请求胜出，重复发起返回拒绝说明。"""
    entry, message = service.create_refresh(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/refresh/complete", response_model=ActionResult)
def complete_refresh(entry_id: int, payload: EntryPayload) -> ActionResult:
    """回填刷新计算结果。

    入参：刷新批次 batch、覆盖对象数 coverage；失败时传 failed=true 与 failure_reason。
    迟到批次、重复完成都会被拒绝；失败不改写分群最终规模与标签覆盖对象数。
    """
    values = payload.values
    try:
        batch = int(values.get("batch"))
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="刷新批次 batch 必须是整数")
    failed = bool(values.get("failed", False))
    coverage = values.get("coverage")
    if coverage is not None and str(coverage).strip() != "":
        try:
            coverage = int(coverage)
        except (TypeError, ValueError):
            return ActionResult(ok=False, message=f"覆盖对象数无法解析为整数：{coverage}")
    else:
        coverage = None
    entry, message = service.complete_refresh(
        entry_id, batch,
        coverage=coverage, failed=failed,
        failure_reason=str(values.get("failure_reason") or ""),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/audit")
def get_audit(entry_id: int) -> dict[str, Any]:
    """读取分群审计台账：从规则提交、刷新批次到生效/失效全程可追溯。"""
    audits = service.get_audit(entry_id)
    if audits is None:
        raise HTTPException(status_code=404, detail=f"人群分群 {entry_id} 不存在或已归档")
    return {"module": "segment", "entry_id": entry_id, "total": len(audits), "items": audits}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分群分析清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "segment", "total": total, "items": items}
