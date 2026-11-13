"""标签分群边界与版本传播验收测试。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v

覆盖验收场景：
1. 最小值等于最大值（边界相等可生效）；
2. 标签换版（已生效分群打回校验中、旧最终规模留存、覆盖对象数不被单侧改写）；
3. 撤销生效（已生效 -> 已失效，规模留存，重试走完整链路）；
4. 重试（刷新失败后重新发起，成功后两侧数字一致）；
并包含异常样例 SEGM-0002 与并发刷新首份胜出。
"""
from __future__ import annotations

import threading
import unittest

from app.services.segment import SegmentService
from app.services.tagmgr import TagmgrService
from app.store import store


class SegmentAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.segments = SegmentService()
        self.tags = TagmgrService()

    def _id(self, code: str, module: str = "segment", field: str = "分群编号") -> int:
        row = store.find_by_code(module, field, code)
        self.assertIsNotNone(row, f"{module} 中找不到 {code}")
        return int(row["id"])

    # ------------------------------------------------------------ 异常样例 SEGM-0002
    def test_segm_0002_rejected_until_tag_published_and_bounds_fixed(self):
        seg_id = self._id("SEGM-0002")
        # 现状：上下限写反（500 > 50）且 TAGM-NIGHT 仍是草稿，生效必须被拒绝
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNone(entry)
        self.assertIn("最小规模 500 大于最大规模 50", message)
        self.assertIn("标签未发布", message)
        self.assertEqual(store.find_by_code("segment", "分群编号", "SEGM-0002")["status"], "规则校验中")

        # 只修上下限、标签仍为草稿：依旧拒绝，原因收敛到标签未发布
        entry, message = self.segments.update_entry(seg_id, {"最小规模": 50, "最大规模": 500})
        self.assertIsNotNone(entry)
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNone(entry)
        self.assertIn("TAGM-NIGHT", message)
        self.assertIn("未发布", message)

        # 发布标签：规则集合引用了草稿属性 DIMP-0004，发布先被拒绝并说明原因
        tag_id = self._id("TAGM-NIGHT", "tagmgr", "标签编号")
        entry, message = self.tags.run_action(tag_id, "发布标签")
        self.assertIsNone(entry)
        self.assertIn("DIMP-0004", message)
        self.assertIn("未发布", message)

        # 属性发布后，标签走 草稿 -> 逻辑评审中 -> 已发布
        dimp4 = store.find_by_code("dimprop", "属性编号", "DIMP-0004")
        dimp4["status"] = "已发布"
        dimp4["pending"] = False
        self.tags.run_action(tag_id, "提交逻辑评审")
        entry, message = self.tags.run_action(tag_id, "发布标签")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["当前版本"], 1)

        # 标签发布但分群还没刷新，缺少与版本对应的最终规模：拒绝
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNone(entry)
        self.assertIn("尚未完成刷新", message)

        # 刷新得到 260 个对象，落在 [50, 500] 区间，首份完整结果同时回写标签覆盖对象数
        entry, message = self.segments.create_refresh(seg_id)
        self.assertIsNotNone(entry)
        batch = entry["刷新批次"]
        entry, message = self.segments.complete_refresh(seg_id, batch, coverage=260)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["最终规模"], 260)
        self.assertEqual(store.find_by_code("tagmgr", "标签编号", "TAGM-NIGHT")["覆盖对象数"], 260)

        # 区间恢复为 50 至 500 且标签已发布，允许进入已生效
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["status"], "已生效")
        self.assertEqual(entry["最小规模"], 50)
        self.assertEqual(entry["最大规模"], 500)

        # 已有 SEGM-LOYAL 记录不参与本次校验，状态与留存规模保持原样
        loyal = store.find_by_code("segment", "分群编号", "SEGM-LOYAL")
        self.assertEqual(loyal["status"], "规则校验中")
        self.assertEqual(loyal["最终规模"], 260)
        self.assertEqual(loyal["历史规模列表"][0]["最终规模"], 260)

    # ------------------------------------------------------------ 场景一：最小值等于最大值
    def test_min_equals_max_boundary(self):
        seg_id = self._id("SEGM-EQUAL")
        seg = store.find("segment", seg_id)
        self.assertEqual(seg["最小规模"], seg["最大规模"])
        # 已生效分群不能重复生效（状态机）
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNone(entry)
        self.assertIn("状态流转被拒绝", message)

        # 失效后重新提交并刷新：覆盖对象数恰好等于上下限 300，允许再次生效
        self.segments.run_action(seg_id, "失效分群")
        self.segments.run_action(seg_id, "提交规则校验")
        entry, _ = self.segments.create_refresh(seg_id)
        entry, message = self.segments.complete_refresh(seg_id, entry["刷新批次"], coverage=300)
        self.assertIsNotNone(entry)
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["status"], "已生效")

        # 差一个也不行：299 低于下限、301 超过上限
        for bad in (299, 301):
            self.segments.run_action(seg_id, "失效分群")
            self.segments.run_action(seg_id, "提交规则校验")
            entry, _ = self.segments.create_refresh(seg_id)
            self.segments.complete_refresh(seg_id, entry["刷新批次"], coverage=bad)
            entry, message = self.segments.run_action(seg_id, "生效分群")
            self.assertIsNone(entry)
            self.assertTrue("低于下限" in message or "超过上限" in message)

    def test_zero_object_rejected(self):
        seg_id = self._id("SEGM-ZERO")
        entry, _ = self.segments.create_refresh(seg_id)
        entry, message = self.segments.complete_refresh(seg_id, entry["刷新批次"], coverage=0)
        self.assertIsNotNone(entry)  # 零对象是合法刷新结果，但不允许生效
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNone(entry)
        self.assertIn("覆盖对象数为 0", message)

    # ------------------------------------------------------------ 场景二：标签换版
    def test_tag_version_change_reactivates_segments_and_keeps_scale(self):
        # 先让 SEGM-EQUAL 处于已生效（种子里已是），最终规模 300
        seg_id = self._id("SEGM-EQUAL")
        tag_id = self._id("TAGM-LOYAL", "tagmgr", "标签编号")
        tag = store.find("tagmgr", tag_id)
        self.assertEqual(tag["当前版本"], 2)
        self.assertEqual(store.find("segment", seg_id)["最终规模"], 300)

        # 编辑已发布标签会退回草稿；重新发布即换版 v3
        self.tags.update_entry(tag_id, {"计算逻辑": "累计消费金额高于新阈值且近14日复购"})
        self.assertEqual(store.find("tagmgr", tag_id)["status"], "草稿")
        self.tags.run_action(tag_id, "提交逻辑评审")
        entry, message = self.tags.run_action(tag_id, "发布标签")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["当前版本"], 3)
        self.assertEqual(len(entry["版本快照"]), 2)  # v1 历史 + 本次留存的 v2 快照

        # 已生效分群被打回规则校验中，旧最终规模 300 原样保留
        seg = store.find("segment", seg_id)
        self.assertEqual(seg["status"], "规则校验中")
        self.assertEqual(seg["最终规模"], 300)
        self.assertEqual(seg["标签版本"], 2)
        self.assertTrue(any(item["最终规模"] == 300 for item in seg["历史规模列表"]))
        self.assertEqual(seg["刷新状态"], "待刷新")

        # 不刷新直接生效：版本不一致必须拒绝
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNone(entry)
        self.assertIn("v2", message)
        self.assertIn("v3", message)

        # 刷新前先放宽规模区间（规则校验中允许修正配置），标签版本对齐 v3，新规模 320 同时回写
        self.segments.update_entry(seg_id, {"最小规模": 100, "最大规模": 500})
        entry, _ = self.segments.create_refresh(seg_id)
        entry, message = self.segments.complete_refresh(seg_id, entry["刷新批次"], coverage=320)
        self.assertIsNotNone(entry)
        self.assertEqual(store.find("segment", seg_id)["标签版本"], 3)
        self.assertEqual(store.find("tagmgr", tag_id)["覆盖对象数"], 320)
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNotNone(entry, message)

        # 换版时在途刷新批次作废，迟到结果不能覆盖新规模：
        # 先让分群重新进入规则校验中并建立在途批次，再触发标签换版
        self.segments.run_action(seg_id, "失效分群")
        self.segments.run_action(seg_id, "提交规则校验")
        entry, _ = self.segments.create_refresh(seg_id)
        stale_batch = entry["刷新批次"]
        # 标签再次换版导致在途批次作废
        self.tags.update_entry(tag_id, {"计算逻辑": "再次调整口径"})
        self.tags.run_action(tag_id, "提交逻辑评审")
        self.tags.run_action(tag_id, "发布标签")
        entry, message = self.segments.complete_refresh(seg_id, stale_batch, coverage=999)
        self.assertIsNone(entry)
        self.assertIn("已作废", message)
        self.assertEqual(store.find("segment", seg_id)["最终规模"], 320)
        self.assertEqual(store.find("tagmgr", tag_id)["覆盖对象数"], 320)

    # ------------------------------------------------------------ 场景三：撤销生效
    def test_deactivate_retains_scale_and_audit(self):
        seg_id = self._id("SEGM-EQUAL")
        entry, message = self.segments.run_action(seg_id, "失效分群")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "已失效")
        self.assertEqual(entry["最终规模"], 300)  # 失效规模留存，不残留为脏数据
        audits = self.segments.get_audit(seg_id)
        self.assertTrue(any(a["动作"] == "失效分群" and a["结果"] == "成功" for a in audits))

        # 已失效不能直接生效，必须先重新提交
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNone(entry)
        self.assertIn("状态流转被拒绝", message)

    # ------------------------------------------------------------ 场景四：重试（失败不单侧改写）
    def test_retry_after_failure_keeps_numbers_consistent(self):
        seg_id = self._id("SEGM-EQUAL")
        tag_id = self._id("TAGM-LOYAL", "tagmgr", "标签编号")
        self.segments.run_action(seg_id, "失效分群")
        self.segments.run_action(seg_id, "提交规则校验")

        # 第一次刷新失败：分群最终规模与标签覆盖对象数都保持原值
        entry, _ = self.segments.create_refresh(seg_id)
        batch = entry["刷新批次"]
        before_scale = store.find("segment", seg_id)["最终规模"]
        before_coverage = store.find("tagmgr", tag_id)["覆盖对象数"]
        entry, message = self.segments.complete_refresh(
            seg_id, batch, failed=True, failure_reason="计算引擎超时"
        )
        self.assertIsNone(entry)
        self.assertIn("计算引擎超时", message)
        self.assertEqual(store.find("segment", seg_id)["最终规模"], before_scale)
        self.assertEqual(store.find("tagmgr", tag_id)["覆盖对象数"], before_coverage)
        self.assertEqual(store.find("segment", seg_id)["刷新状态"], "失败")

        # 重试：重新发起刷新并成功，两侧数字同步更新
        entry, _ = self.segments.create_refresh(seg_id)
        new_batch = entry["刷新批次"]
        self.assertGreater(new_batch, batch)
        entry, message = self.segments.complete_refresh(seg_id, new_batch, coverage=300)
        self.assertIsNotNone(entry, message)
        self.assertEqual(store.find("segment", seg_id)["最终规模"], 300)
        self.assertEqual(store.find("tagmgr", tag_id)["覆盖对象数"], 300)

    # ------------------------------------------------------------ 并发：首份完整结果胜出
    def test_concurrent_refresh_first_complete_wins(self):
        seg_id = self._id("SEGM-ZERO")
        entry, _ = self.segments.create_refresh(seg_id)
        batch = entry["刷新批次"]

        # 计算中再次发起：被幂等拒绝
        entry, message = self.segments.create_refresh(seg_id)
        self.assertIsNone(entry)
        self.assertIn("首份请求", message)

        winner: list[tuple[object, str]] = []
        barrier = threading.Barrier(2)

        def complete(count: int) -> None:
            barrier.wait()
            winner.append(self.segments.complete_refresh(seg_id, batch, coverage=count))

        t1 = threading.Thread(target=complete, args=(120,))
        t2 = threading.Thread(target=complete, args=(210,))
        t1.start(); t2.start(); t1.join(); t2.join()

        ok = [item for item in winner if item[0] is not None]
        rejected = [item for item in winner if item[0] is None]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(rejected), 1)
        self.assertIn("已有完整结果胜出", rejected[0][1])
        winning_count = ok[0][0]["最终规模"]
        self.assertIn(winning_count, (120, 210))
        # 标签覆盖对象数与分群最终规模严格一致，且只回写一次
        tag = store.find_by_code("tagmgr", "标签编号", "TAGM-LOYAL")
        self.assertEqual(tag["覆盖对象数"], winning_count)
        writebacks = [
            a for a in self.segments.get_audit(seg_id)
            if a["动作"] == "刷新完成"
        ]
        self.assertEqual(len(writebacks), 1)

    # ------------------------------------------------------------ 规则集合失效属性
    def test_broken_rule_set_blocks_publish_and_activation(self):
        broken_id = self._id("TAGM-BROKEN", "tagmgr", "标签编号")
        entry, message = self.tags.run_action(broken_id, "发布标签")
        self.assertIsNone(entry)
        self.assertIn("DIMP-9999", message)

        # 建一个引用失效标签的分群，生效时同样要拦下并说明属性失效
        entry, errors = self.segments.create_entry({
            "分群编号": "SEGM-BROKEN",
            "分群名称": "失效属性规则分群",
            "筛选规则": "引用不存在的属性",
            "关联标签编号": "TAGM-BROKEN",
            "最小规模": 10,
            "最大规模": 100,
        })
        self.assertIsNotNone(entry, errors)
        seg_id = entry["id"]
        self.segments.run_action(seg_id, "提交规则校验")
        entry, message = self.segments.run_action(seg_id, "生效分群")
        self.assertIsNone(entry)
        self.assertIn("DIMP-9999", message)

        # 下线标签同样不可生效
        off_tag = self._id("TAGM-OFFLINE", "tagmgr", "标签编号")
        self.assertEqual(store.find("tagmgr", off_tag)["status"], "已下线")

    # ------------------------------------------------------------ 追溯：标签定义 -> 分群规模 -> 审计
    def test_trace_from_tag_to_segment_scale_and_audit(self):
        trace = self.tags.trace(self._id("TAGM-LOYAL", "tagmgr", "标签编号"))
        self.assertIsNotNone(trace)
        codes = {seg["分群编号"] for seg in trace["引用分群"]}
        self.assertIn("SEGM-LOYAL", codes)
        self.assertIn("SEGM-EQUAL", codes)
        loyal = next(seg for seg in trace["引用分群"] if seg["分群编号"] == "SEGM-LOYAL")
        self.assertEqual(loyal["最终规模"], 260)
        self.assertTrue(loyal["审计"])  # 分群侧审计可追到
        self.assertTrue(trace["标签审计"])  # 标签侧审计可追到
        self.assertTrue(trace["版本快照"])

    def test_create_validation_min_gt_max_and_duplicate_submit(self):
        entry, errors = self.segments.create_entry({
            "分群编号": "SEGM-BAD",
            "分群名称": "上下限写反",
            "筛选规则": "x",
            "关联标签编号": "TAGM-LOYAL",
            "最小规模": 99,
            "最大规模": 1,
        })
        self.assertIsNone(entry)
        self.assertTrue(any("不能大于" in e for e in errors))

        # 同规则重复提交幂等：新建草稿分群，正式提交建立指纹后再次提交不产生新批次
        entry, errors = self.segments.create_entry({
            "分群编号": "SEGM-IDEM",
            "分群名称": "幂等提交演示",
            "筛选规则": "高忠诚用户标签=1",
            "关联标签编号": "TAGM-LOYAL",
            "最小规模": 10,
            "最大规模": 100,
        })
        self.assertIsNotNone(entry, errors)
        seg_id = entry["id"]
        entry, message = self.segments.run_action(seg_id, "提交规则校验")
        self.assertIsNotNone(entry)
        batch_after_first = store.find("segment", seg_id)["刷新批次"]
        entry, message = self.segments.run_action(seg_id, "提交规则校验")
        self.assertIsNotNone(entry)
        self.assertIn("幂等", message)
        self.assertEqual(store.find("segment", seg_id)["刷新批次"], batch_after_first)


if __name__ == "__main__":
    unittest.main()
