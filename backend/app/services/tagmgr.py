"""标签管理业务规则：版本管理、规则集合校验、换版对分群的级联。

数据流定位：标签是分群生效的上游。分群引用的标签必须“已发布 + 版本一致 + 规则集合
引用的维度属性全部有效”，任一条件不满足都由分群服务在生效校验时拒绝；标签换版时本
服务负责把已生效分群打回“规则校验中”，但保留其旧最终规模供追溯。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.audit import audit_service
from app.store import store

MODULE = "tagmgr"
DIMP_MODULE = "dimprop"
SEGMENT_MODULE = "segment"
REQUIRED_FIELDS = ["标签编号", "标签名称", "标签分类"]
STATUS_ORDER = ["草稿", "逻辑评审中", "已发布", "已下线"]
ACTION_RULES = {"提交逻辑评审": "逻辑评审中", "发布标签": "已发布", "下线标签": "已下线"}
NEGATIVE_ACTIONS = ["下线标签"]
EDITABLE_STATUSES = ("草稿", "逻辑评审中", "已下线")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def validate_rule_set(rule_codes: list[str]) -> tuple[bool, str]:
    """校验标签规则集合引用的维度属性：必须非空、存在且处于已发布状态。

    供标签发布与分群生效两处复用，保证口径一致。
    """
    if not rule_codes:
        return False, "规则集合为空，标签至少需要引用一个维度属性"
    seen: set[str] = set()
    for code in rule_codes:
        code = str(code or "").strip()
        if not code:
            return False, "规则集合中存在空的属性引用"
        if code in seen:
            return False, f"规则集合重复引用属性 {code}"
        seen.add(code)
        attr = store.find_by_code(DIMP_MODULE, "属性编号", code)
        if attr is None:
            return False, f"规则集合引用的属性 {code} 不存在，引用失效"
        if attr.get("status") != "已发布":
            return False, f"规则集合引用的属性 {code} 状态为「{attr.get('status')}」，未发布不可用"
    return True, ""


class TagmgrService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("标签编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_by_code(self, code: str) -> dict[str, Any] | None:
        return store.find_by_code(MODULE, "标签编号", code)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        code = str(values["标签编号"]).strip()
        if self.get_by_code(code) is not None:
            return None, [f"标签编号 {code} 已存在"]
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["计算逻辑"] = values.get("计算逻辑") or ""
        entry["值类型"] = values.get("值类型") or "数值型"
        entry["刷新周期"] = values.get("刷新周期") or "每日"
        entry["规则集合"] = [str(item).strip() for item in (values.get("规则集合") or []) if str(item).strip()]
        entry["当前版本"] = 0
        entry["覆盖对象数"] = None
        entry["最近计算时间"] = None
        entry["版本快照"] = []
        rows.append(entry)
        audit_service.record(
            module=MODULE, entry_id=entry["id"], code=code,
            action="登记标签", ok=True, detail="标签草稿已登记", snapshot=self._public(entry),
        )
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改标签定义。已发布标签被修改时自动退回草稿，作为待发布的新版本。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"标签定义 {entry_id} 不存在或已归档"
        with store.lock:
            was_published = entry.get("status") == "已发布"
            if not was_published and entry.get("status") not in EDITABLE_STATUSES:
                return None, f"标签当前状态「{entry.get('status')}」不允许编辑"
            for field in ("标签名称", "标签分类", "计算逻辑", "值类型", "刷新周期"):
                if field in values and str(values.get(field) or "").strip():
                    entry[field] = str(values[field]).strip()
            if "规则集合" in values:
                rule_codes = [str(item).strip() for item in (values.get("规则集合") or []) if str(item).strip()]
                entry["规则集合"] = rule_codes
            if was_published:
                entry["status"] = "草稿"
                entry["pending"] = True
                reason = "已发布标签定义被修改，退回草稿等待换版发布"
            elif entry.get("status") == "逻辑评审中":
                entry["status"] = "草稿"
                entry["pending"] = True
                reason = "评审中的标签定义被修改，退回草稿重新评审"
            else:
                reason = "标签草稿已更新"
            audit_service.record(
                module=MODULE, entry_id=entry_id, code=str(entry.get("标签编号")),
                action="编辑标签", ok=True, detail=reason, snapshot=self._public(entry),
            )
            return entry, reason

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"标签定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于标签管理可执行范围"
        target = ACTION_RULES[action]
        current = str(entry.get("status"))
        with store.transaction():
            if action == "发布标签":
                ok, reason = validate_rule_set(list(entry.get("规则集合") or []))
                if not ok:
                    audit_service.record(
                        module=MODULE, entry_id=entry_id, code=str(entry.get("标签编号")),
                        action=action, ok=False, detail=f"发布被拒绝：{reason}",
                        snapshot=self._public(entry),
                    )
                    return None, f"标签发布被拒绝：{reason}"
                if current not in ("逻辑评审中", "草稿"):
                    return None, f"标签当前状态「{current}」不允许发布，需先提交逻辑评审"
                self._publish(entry)
                message = f"标签 {entry['标签编号']} 已发布 v{entry['当前版本']}"
            elif action == "提交逻辑评审":
                if current != "草稿":
                    return None, f"标签当前状态「{current}」不允许提交逻辑评审，仅草稿可提交"
                entry["status"] = target
                entry["pending"] = True
                message = f"标签定义已{action}"
            else:  # 下线标签
                if current != "已发布":
                    return None, f"标签当前状态「{current}」不允许下线，仅已发布标签可下线"
                entry["status"] = target
                entry["pending"] = False
                entry["abnormal"] = True
                message = f"标签定义已{action}"
            audit_service.record(
                module=MODULE, entry_id=entry_id, code=str(entry.get("标签编号")),
                action=action, ok=True, detail=message, snapshot=self._public(entry),
            )
            return entry, message

    def _publish(self, entry: dict[str, Any]) -> None:
        """发布即换版：版本号 +1、留存旧版本快照、级联打回引用它的已生效分群。"""
        old_version = int(entry.get("当前版本") or 0)
        if old_version > 0:
            entry.setdefault("版本快照", []).append({
                "版本": old_version,
                "规则集合": list(entry.get("规则集合") or []),
                "覆盖对象数": entry.get("覆盖对象数"),
                "发布时间": entry.get("最近计算时间"),
                "留存时间": _now(),
            })
        entry["当前版本"] = old_version + 1
        entry["status"] = "已发布"
        entry["pending"] = False
        entry["abnormal"] = False
        if old_version > 0:
            self._cascade_segment_version(entry)

    def _cascade_segment_version(self, tag: dict[str, Any]) -> None:
        """换版级联：已生效分群回到规则校验中，旧最终规模原样保留并登记追溯。

        任何引用该标签的分群（含规则校验中正在刷新的），在途刷新批次一律作废，
        保证基于旧版本计算的迟到结果无法落库。
        """
        tag_code = str(tag.get("标签编号"))
        new_version = int(tag.get("当前版本") or 0)
        for seg in store.rows(SEGMENT_MODULE):
            if str(seg.get("关联标签编号") or "") != tag_code:
                continue
            # 作废所有在途刷新批次，迟到的完成结果将因批次不匹配被拒绝
            seg["刷新批次"] = int(seg.get("刷新批次") or 0) + 1
            if seg.get("刷新状态") == "计算中":
                seg["刷新状态"] = "待刷新"
            if seg.get("status") != "已生效":
                continue
            old_scale = seg.get("最终规模")
            seg.setdefault("历史规模列表", []).append({
                "标签版本": seg.get("标签版本"),
                "最终规模": old_scale,
                "留存时间": _now(),
                "原因": f"标签 {tag_code} 换版至 v{new_version}，换版前最终规模留存追溯",
            })
            seg["status"] = "规则校验中"
            seg["pending"] = True
            seg["abnormal"] = False
            seg["刷新状态"] = "待刷新"
            seg["校验原因"] = f"标签 {tag_code} 已换版至 v{new_version}，分群仍引用 v{seg.get('标签版本')}，需重新刷新校验"
            audit_service.record(
                module=SEGMENT_MODULE, entry_id=int(seg.get("id", 0)),
                code=str(seg.get("分群编号")), action="标签换版打回", ok=True,
                detail=f"{seg['校验原因']}；旧最终规模 {old_scale} 已留存",
                snapshot={"最终规模": old_scale, "标签版本": seg.get("标签版本"), "status": seg["status"]},
            )

    def apply_coverage(self, tag: dict[str, Any], count: int, segment_code: str) -> None:
        """分群刷新成功后回写标签覆盖对象数。仅允许在 store 事务内调用。"""
        tag["覆盖对象数"] = count
        tag["最近计算时间"] = _now()
        audit_service.record(
            module=MODULE, entry_id=int(tag.get("id", 0)), code=str(tag.get("标签编号")),
            action="覆盖对象数回写", ok=True,
            detail=f"分群 {segment_code} 刷新成功，覆盖对象数回写为 {count}",
            snapshot=self._public(tag),
        )

    def trace(self, entry_id: int) -> dict[str, Any] | None:
        """追溯视图：从标签定义出发，串起版本快照、引用分群的规模与审计。"""
        tag = store.find(MODULE, entry_id)
        if tag is None:
            return None
        tag_code = str(tag.get("标签编号"))
        segments = [
            dict(seg) for seg in store.rows(SEGMENT_MODULE)
            if str(seg.get("关联标签编号") or "") == tag_code
        ]
        for seg in segments:
            seg["审计"] = audit_service.list_for(SEGMENT_MODULE, int(seg["id"]))
        return {
            "标签": self._public(tag),
            "标签审计": audit_service.list_for(MODULE, entry_id),
            "版本快照": list(tag.get("版本快照") or []),
            "引用分群": segments,
        }

    @staticmethod
    def _public(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "标签编号": entry.get("标签编号"),
            "status": entry.get("status"),
            "当前版本": entry.get("当前版本"),
            "规则集合": list(entry.get("规则集合") or []),
            "覆盖对象数": entry.get("覆盖对象数"),
            "最近计算时间": entry.get("最近计算时间"),
        }
