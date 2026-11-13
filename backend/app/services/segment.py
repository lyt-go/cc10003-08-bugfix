"""分群分析业务规则：规模边界校验、严格状态机、刷新并发控制与标签回写。

数据流（分群生效 → 标签回写）：
1. 提交规则校验：草稿 -> 规则校验中，同规则指纹重复提交直接幂等返回。
2. 刷新分群：创建刷新批次 -> 计算覆盖对象数 -> 首份完整结果在同一事务内同时改写
   分群最终规模与标签覆盖对象数；失败整体回滚，两侧数字都不会被单侧改写。
3. 生效分群：标签版本和人群最终规模共同决定能否生效，五类拒绝原因逐条说明。
4. 失效分群：已生效 -> 已失效，最终规模留存；重新提交即重试，走完整校验链路。
5. 标签换版：tagmgr 服务把已生效分群打回规则校验中，旧最终规模进入历史规模列表。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.services.audit import audit_service
from app.services.tagmgr import TagmgrService, validate_rule_set
from app.store import store

MODULE = "segment"
TAGM_MODULE = "tagmgr"
REQUIRED_FIELDS = ["分群编号", "分群名称", "筛选规则", "关联标签编号", "最小规模", "最大规模"]
STATUS_ORDER = ["草稿", "规则校验中", "已生效", "已失效"]
# 状态机：只允许沿 草稿 -> 规则校验中 -> 已生效 -> 已失效 推进；
# 已失效可重新提交回到规则校验中（重试场景），其余逆向跳转一律拒绝。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "草稿": {"规则校验中"},
    "规则校验中": {"已生效"},
    "已生效": {"已失效"},
    "已失效": {"规则校验中"},
}
ACTION_TARGET = {"提交规则校验": "规则校验中", "生效分群": "已生效", "失效分群": "已失效"}
EDITABLE_STATUSES = ("草稿", "规则校验中", "已失效")
REFRESH_STATUSES = ("规则校验中",)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_int(value: Any, field: str) -> tuple[int | None, str]:
    """把规模字段转成非负整数；无法解析时返回可读原因。"""
    if value is None or str(value).strip() == "":
        return None, f"{field}不能为空"
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None, f"{field}必须是整数，当前值「{value}」无法解析"
    if number < 0:
        return None, f"{field}不能为负数，当前值为 {number}"
    return number, ""


class SegmentService:
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
            rows = [row for row in rows if keyword in str(row.get("分群编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ------------------------------------------------------------------ 登记/编辑
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        min_size, min_err = _to_int(values.get("最小规模"), "最小规模")
        max_size, max_err = _to_int(values.get("最大规模"), "最大规模")
        if min_err or max_err:
            return None, [msg for msg in (min_err, max_err) if msg]
        assert min_size is not None and max_size is not None
        if min_size > max_size:
            return None, [f"最小规模 {min_size} 不能大于最大规模 {max_size}"]
        rows = store.rows(MODULE)
        code = str(values["分群编号"]).strip()
        if any(str(row.get("分群编号") or "") == code for row in rows):
            return None, [f"分群编号 {code} 已存在"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in ("分群编号", "分群名称", "筛选规则")})
        entry["关联标签编号"] = str(values["关联标签编号"]).strip()
        entry["最小规模"] = min_size
        entry["最大规模"] = max_size
        entry["刷新周期"] = values.get("刷新周期") or "每日"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["标签版本"] = None
        entry["最终规模"] = None
        entry["历史规模列表"] = []
        entry["刷新状态"] = "待刷新"
        entry["刷新批次"] = 0
        entry["规则指纹"] = self._fingerprint(entry)
        entry["最近计算时间"] = None
        entry["校验原因"] = ""
        rows.append(entry)
        audit_service.record(
            module=MODULE, entry_id=entry["id"], code=entry["分群编号"],
            action="登记分群", ok=True, detail="分群草稿已登记", snapshot=self._snapshot(entry),
        )
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改分群配置。仅草稿/已失效可改；改动后清空旧刷新结果，避免失效规模残留。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"人群分群 {entry_id} 不存在或已归档"
        if entry.get("status") not in EDITABLE_STATUSES:
            return None, f"分群当前状态「{entry.get('status')}」不允许编辑，仅草稿、规则校验中或已失效可修改"
        min_size = entry["最小规模"]
        max_size = entry["最大规模"]
        if "最小规模" in values and str(values.get("最小规模") or "").strip():
            parsed, err = _to_int(values["最小规模"], "最小规模")
            if err:
                return None, err
            min_size = parsed
        if "最大规模" in values and str(values.get("最大规模") or "").strip():
            parsed, err = _to_int(values["最大规模"], "最大规模")
            if err:
                return None, err
            max_size = parsed
        if min_size > max_size:
            return None, f"最小规模 {min_size} 不能大于最大规模 {max_size}"
        with store.lock:
            entry["最小规模"] = min_size
            entry["最大规模"] = max_size
            for field in ("分群名称", "筛选规则", "关联标签编号", "刷新周期"):
                if field in values and str(values.get(field) or "").strip():
                    entry[field] = str(values[field]).strip()
            entry["规则指纹"] = self._fingerprint(entry)
            entry["标签版本"] = None
            entry["最终规模"] = None
            entry["刷新状态"] = "待刷新"
            entry["刷新批次"] = int(entry.get("刷新批次") or 0) + 1
            entry["最近计算时间"] = None
            entry["校验原因"] = "分群配置已修改，旧刷新结果作废，需重新提交校验"
            audit_service.record(
                module=MODULE, entry_id=entry_id, code=str(entry.get("分群编号")),
                action="编辑分群", ok=True, detail=entry["校验原因"], snapshot=self._snapshot(entry),
            )
            return entry, "分群配置已更新"

    # ------------------------------------------------------------------ 状态机
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"人群分群 {entry_id} 不存在或已归档"
        if action not in ACTION_TARGET:
            return None, f"动作「{action}」不属于分群分析可执行范围"
        target = ACTION_TARGET[action]
        current = str(entry.get("status"))
        # 重复提交同一规则：分群已在规则校验中且规则指纹未变时幂等返回，不产生新批次
        if action == "提交规则校验" and current == "规则校验中":
            if entry.get("规则指纹") == self._fingerprint(entry):
                return entry, "相同规则已在校验中，重复提交已幂等忽略"
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            return None, (
                f"状态流转被拒绝：分群当前为「{current}」，只允许推进到"
                f"{'、'.join(sorted(ALLOWED_TRANSITIONS.get(current, set()))) or '无'}，"
                f"不能直接变为「{target}」"
            )
        if action == "提交规则校验":
            return self._submit(entry)
        if action == "生效分群":
            return self._activate(entry)
        return self._deactivate(entry)

    def _submit(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """提交规则校验：边界预检 + 同规则指纹幂等，重复提交不产生新批次。"""
        with store.lock:
            min_size, max_size = int(entry["最小规模"]), int(entry["最大规模"])
            if min_size > max_size:
                reason = f"最小规模 {min_size} 大于最大规模 {max_size}，规模上下限非法"
                self._reject(entry, "提交规则校验", reason)
                return None, f"提交规则校验被拒绝：{reason}"
            tag = self._resolve_tag(entry)
            if tag is None:
                reason = f"关联标签 {entry.get('关联标签编号')} 不存在，规则集合引用失效"
                self._reject(entry, "提交规则校验", reason)
                return None, f"提交规则校验被拒绝：{reason}"
            fingerprint = self._fingerprint(entry)
            if (
                entry.get("规则指纹") == fingerprint
                and entry.get("status") == "规则校验中"
            ):
                return entry, "相同规则已在校验中，重复提交已幂等忽略"
            entry["规则指纹"] = fingerprint
            entry["status"] = "规则校验中"
            entry["pending"] = True
            entry["abnormal"] = False
            entry["刷新状态"] = "待刷新"
            entry["刷新批次"] = int(entry.get("刷新批次") or 0) + 1
            entry["校验原因"] = ""
            audit_service.record(
                module=MODULE, entry_id=int(entry["id"]), code=str(entry.get("分群编号")),
                action="提交规则校验", ok=True,
                detail=f"分群进入规则校验中，刷新批次 {entry['刷新批次']}",
                snapshot=self._snapshot(entry),
            )
            return entry, "人群分群已提交规则校验"

    def _activate(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """生效校验：标签版本与人群最终规模共同决定，五类原因任一不满足即拒绝。"""
        with store.lock:
            reasons = self._validate_activation(entry)
            if reasons:
                detail = "；".join(reasons)
                self._reject(entry, "生效分群", detail)
                return None, f"生效分群被拒绝：{detail}"
            tag = self._resolve_tag(entry)
            assert tag is not None
            entry["status"] = "已生效"
            entry["pending"] = False
            entry["abnormal"] = False
            entry["校验原因"] = ""
            message = (
                f"分群 {entry['分群编号']} 已生效：标签 {tag['标签编号']} v{entry['标签版本']}，"
                f"最终规模 {entry['最终规模']} 落在区间 [{entry['最小规模']}, {entry['最大规模']}]"
            )
            audit_service.record(
                module=MODULE, entry_id=int(entry["id"]), code=str(entry.get("分群编号")),
                action="生效分群", ok=True, detail=message, snapshot=self._snapshot(entry),
            )
            return entry, message

    def _validate_activation(self, entry: dict[str, Any]) -> list[str]:
        """返回全部拒绝原因（空列表代表可生效），一次说清所有问题。"""
        reasons: list[str] = []
        min_size, min_err = _to_int(entry.get("最小规模"), "最小规模")
        max_size, max_err = _to_int(entry.get("最大规模"), "最大规模")
        if min_err:
            reasons.append(min_err)
        if max_err:
            reasons.append(max_err)
        if min_size is not None and max_size is not None and min_size > max_size:
            reasons.append(f"最小规模 {min_size} 大于最大规模 {max_size}，规模上下限写反")
        tag = self._resolve_tag(entry)
        if tag is None:
            reasons.append(f"关联标签 {entry.get('关联标签编号')} 不存在，规则集合引用失效")
        else:
            if tag.get("status") != "已发布":
                reasons.append(f"关联标签 {tag.get('标签编号')} 当前为「{tag.get('status')}」，标签未发布不可生效")
            else:
                tag_version = int(tag.get("当前版本") or 0)
                seg_version = entry.get("标签版本")
                if seg_version is None:
                    reasons.append("分群尚未完成刷新，缺少与标签版本对应的最终规模")
                elif int(seg_version) != tag_version:
                    reasons.append(
                        f"分群基于标签 v{seg_version} 计算，标签当前已是 v{tag_version}，版本不一致需重新刷新"
                    )
            ok, rule_err = validate_rule_set(list(tag.get("规则集合") or []))
            if not ok:
                reasons.append(f"标签规则集合校验失败：{rule_err}")
        final_scale = entry.get("最终规模")
        if final_scale is None:
            reasons.append("分群尚未刷新出最终规模（覆盖对象数为空），不能生效")
        else:
            scale = int(final_scale)
            if scale == 0:
                reasons.append("覆盖对象数为 0，零对象分群不允许生效")
            if min_size is not None and scale < min_size:
                reasons.append(f"覆盖对象数 {scale} 小于最小规模 {min_size}，低于下限")
            if max_size is not None and scale > max_size:
                reasons.append(f"覆盖对象数 {scale} 大于最大规模 {max_size}，超过上限")
        return reasons

    def _deactivate(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """失效分群：最终规模原样留存供追溯，仅状态与待处理标记变化。"""
        with store.lock:
            retained = entry.get("最终规模")
            entry["status"] = "已失效"
            entry["pending"] = False
            entry["abnormal"] = True
            entry["刷新状态"] = "已失效"
            entry["校验原因"] = "分群被手动失效，最终规模留存追溯"
            message = f"分群 {entry['分群编号']} 已失效，最终规模 {retained} 留存供追溯"
            audit_service.record(
                module=MODULE, entry_id=int(entry["id"]), code=str(entry.get("分群编号")),
                action="失效分群", ok=True, detail=message, snapshot=self._snapshot(entry),
            )
            return entry, message

    # ------------------------------------------------------------------ 刷新链路
    def create_refresh(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """发起一次覆盖计算。并发/重复发起时首份在途批次胜出，后续请求被幂等拒绝。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"人群分群 {entry_id} 不存在或已归档"
        with store.lock:
            if entry.get("status") not in REFRESH_STATUSES:
                return None, (
                    f"仅规则校验中的分群可以刷新，当前状态「{entry.get('status')}」；"
                    "请先提交规则校验"
                )
            if entry.get("刷新状态") == "计算中":
                return None, (
                    f"刷新批次 {entry.get('刷新批次')} 仍在计算中，"
                    "并发刷新同一分群只接受首份请求，请等待其完成"
                )
            entry["刷新状态"] = "计算中"
            entry["刷新批次"] = int(entry.get("刷新批次") or 0) + 1
            entry["校验原因"] = ""
            batch = int(entry["刷新批次"])
            audit_service.record(
                module=MODULE, entry_id=entry_id, code=str(entry.get("分群编号")),
                action="发起刷新", ok=True, detail=f"刷新批次 {batch} 开始计算",
                snapshot=self._snapshot(entry),
            )
            return {"分群编号": entry["分群编号"], "刷新批次": batch, "刷新状态": "计算中"}, "刷新任务已创建"

    def complete_refresh(
        self,
        entry_id: int,
        batch: int,
        *,
        coverage: int | None = None,
        failed: bool = False,
        failure_reason: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """完成一次刷新计算。

        并发控制：批次号与当前在途批次不一致（被标签换版/重新提交作废）的迟到结果直接丢弃；
        同一批次首份完整结果胜出，重复完成被拒绝。失败时事务回滚，标签覆盖对象数不会被
        单侧改写。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"人群分群 {entry_id} 不存在或已归档"
        with store.lock:
            current_batch = int(entry.get("刷新批次") or 0)
            if int(batch) != current_batch:
                return None, (
                    f"刷新批次 {batch} 已作废（当前批次 {current_batch}），"
                    "迟到结果已丢弃，不影响现有规模"
                )
            if entry.get("刷新状态") != "计算中":
                return None, (
                    f"批次 {batch} 已有完整结果胜出（状态 {entry.get('刷新状态')}），"
                    "重复完成请求被拒绝"
                )
            tag = self._resolve_tag(entry)
            if tag is None:
                failed = True
                failure_reason = failure_reason or f"关联标签 {entry.get('关联标签编号')} 不存在，引用失效"
            elif tag.get("status") != "已发布":
                failed = True
                failure_reason = (
                    failure_reason
                    or f"关联标签 {tag.get('标签编号')} 状态为「{tag.get('status')}」，标签未发布不可计算"
                )
            else:
                ok, rule_err = validate_rule_set(list(tag.get("规则集合") or []))
                if not ok:
                    failed = True
                    failure_reason = failure_reason or f"标签规则集合引用失效属性：{rule_err}"
            if failed:
                return self._fail_refresh(entry, tag, batch, failure_reason or "刷新计算失败")
            if coverage is None:
                return self._fail_refresh(entry, tag, batch, "刷新结果缺少覆盖对象数")
            count, err = _to_int(coverage, "覆盖对象数")
            if err or count is None:
                return self._fail_refresh(entry, tag, batch, err or "覆盖对象数非法")
            if count < 0:
                return self._fail_refresh(entry, tag, batch, f"覆盖对象数不能为负：{count}")
            return self._succeed_refresh(entry, tag, batch, count)

    def _succeed_refresh(
        self, entry: dict[str, Any], tag: dict[str, Any], batch: int, count: int
    ) -> tuple[dict[str, Any] | None, str]:
        """成功路径：分群最终规模与标签覆盖对象数在同一事务内落库，任一失败整体回滚。"""
        old_scale = entry.get("最终规模")
        tag_old = tag.get("覆盖对象数")
        try:
            with store.transaction():
                if old_scale is not None:
                    entry.setdefault("历史规模列表", []).append({
                        "标签版本": entry.get("标签版本"),
                        "最终规模": old_scale,
                        "留存时间": _now(),
                        "原因": f"刷新批次 {batch} 产出新规模，旧规模留存追溯",
                    })
                entry["最终规模"] = count
                entry["标签版本"] = int(tag.get("当前版本") or 0)
                entry["刷新状态"] = "已完成"
                entry["最近计算时间"] = _now()
                entry["校验原因"] = ""
                TagmgrService().apply_coverage(tag, count, str(entry.get("分群编号")))
                audit_service.record(
                    module=MODULE, entry_id=int(entry["id"]), code=str(entry.get("分群编号")),
                    action="刷新完成", ok=True,
                    detail=(
                        f"批次 {batch} 首份完整结果胜出，最终规模 {count}，"
                        f"标签覆盖对象数同步回写（{tag_old} -> {count}）"
                    ),
                    snapshot=self._snapshot(entry),
                )
        except Exception as exc:  # pragma: no cover - 事务保证，防御性兜底
            return None, f"刷新落库失败已回滚：{exc}，分群规模与标签覆盖对象数均未改写"
        return entry, f"刷新完成：最终规模 {count}，标签 {tag.get('标签编号')} 覆盖对象数已同步"

    def _fail_refresh(
        self,
        entry: dict[str, Any],
        tag: dict[str, Any] | None,
        batch: int,
        reason: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """失败路径：只标记刷新失败并留审计，最终规模与标签覆盖对象数一律不动。"""
        with store.transaction():
            entry["刷新状态"] = "失败"
            entry["校验原因"] = reason
            audit_service.record(
                module=MODULE, entry_id=int(entry["id"]), code=str(entry.get("分群编号")),
                action="刷新失败", ok=False,
                detail=f"批次 {batch} 计算失败：{reason}；最终规模与标签覆盖对象数保持原值",
                snapshot=self._snapshot(entry),
            )
        return None, f"刷新失败：{reason}；分群最终规模与标签覆盖对象数均未改写"

    def get_audit(self, entry_id: int) -> list[dict[str, Any]] | None:
        if store.find(MODULE, entry_id) is None:
            return None
        return audit_service.list_for(MODULE, entry_id)

    # ------------------------------------------------------------------ 辅助
    def _resolve_tag(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        return store.find_by_code(TAGM_MODULE, "标签编号", str(entry.get("关联标签编号") or ""))

    def _reject(self, entry: dict[str, Any], action: str, reason: str) -> None:
        """拒绝也要落审计，并把原因挂到分群上，前端刷新/重进后仍能看到同一条说明。"""
        entry["校验原因"] = reason
        audit_service.record(
            module=MODULE, entry_id=int(entry["id"]), code=str(entry.get("分群编号")),
            action=action, ok=False, detail=f"{action}被拒绝：{reason}",
            snapshot=self._snapshot(entry),
        )

    @staticmethod
    def _fingerprint(entry: dict[str, Any]) -> str:
        """规则指纹：筛选规则 + 标签 + 规模区间共同决定，任一变化都算新规则。"""
        raw = "|".join([
            str(entry.get("筛选规则") or ""),
            str(entry.get("关联标签编号") or ""),
            str(entry.get("最小规模") or ""),
            str(entry.get("最大规模") or ""),
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _snapshot(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "分群编号": entry.get("分群编号"),
            "status": entry.get("status"),
            "最小规模": entry.get("最小规模"),
            "最大规模": entry.get("最大规模"),
            "最终规模": entry.get("最终规模"),
            "标签版本": entry.get("标签版本"),
            "刷新状态": entry.get("刷新状态"),
            "刷新批次": entry.get("刷新批次"),
            "校验原因": entry.get("校验原因"),
        }
