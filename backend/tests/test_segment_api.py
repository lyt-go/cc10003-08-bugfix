"""分群/标签接口层端到端测试：验证 HTTP 契约、拒绝原因透传与刷新回调链路。"""
from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.store import store


class SegmentApiTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.client = TestClient(app)

    def _seg_id(self, code: str) -> int:
        return int(store.find_by_code("segment", "分群编号", code)["id"])

    def test_segm_0002_full_flow_over_http(self) -> None:
        seg_id = self._seg_id("SEGM-0002")

        # 异常样例：上下限写反 + 标签草稿，生效被拒且原因完整透传
        resp = self.client.post(f"/api/segment/{seg_id}/actions", json={"values": {"action": "生效分群"}})
        self.assertEqual(resp.json()["ok"], False)
        message = resp.json()["message"]
        self.assertIn("最小规模 500 大于最大规模 50", message)
        self.assertIn("标签未发布", message)

        # 修正上下限（规则校验中允许编辑）
        resp = self.client.put(f"/api/segment/{seg_id}", json={"values": {"最小规模": 50, "最大规模": 500}})
        self.assertEqual(resp.json()["ok"], True)

        # 发布标签先因引用草稿属性被拒
        tag_id = int(store.find_by_code("tagmgr", "标签编号", "TAGM-NIGHT")["id"])
        resp = self.client.post(f"/api/tagmgr/{tag_id}/actions", json={"values": {"action": "发布标签"}})
        self.assertEqual(resp.json()["ok"], False)
        self.assertIn("DIMP-0004", resp.json()["message"])

        dimp4 = store.find_by_code("dimprop", "属性编号", "DIMP-0004")
        dimp4["status"] = "已发布"
        dimp4["pending"] = False
        self.client.post(f"/api/tagmgr/{tag_id}/actions", json={"values": {"action": "提交逻辑评审"}})
        resp = self.client.post(f"/api/tagmgr/{tag_id}/actions", json={"values": {"action": "发布标签"}})
        self.assertEqual(resp.json()["ok"], True)

        # 发起刷新 -> 回填 260 -> 生效
        resp = self.client.post(f"/api/segment/{seg_id}/refresh")
        self.assertEqual(resp.json()["ok"], True)
        batch = resp.json()["entry"]["刷新批次"]
        resp = self.client.post(
            f"/api/segment/{seg_id}/refresh/complete",
            json={"values": {"batch": batch, "coverage": 260}},
        )
        self.assertEqual(resp.json()["ok"], True)
        resp = self.client.post(f"/api/segment/{seg_id}/actions", json={"values": {"action": "生效分群"}})
        self.assertEqual(resp.json()["ok"], True)
        self.assertEqual(resp.json()["entry"]["status"], "已生效")

        # 标签覆盖对象数与分群最终规模一致
        tag = self.client.get(f"/api/tagmgr/{tag_id}").json()
        self.assertEqual(tag["覆盖对象数"], 260)
        seg = self.client.get(f"/api/segment/{seg_id}").json()
        self.assertEqual(seg["最终规模"], 260)

        # 审计与追溯接口可从标签定义追到分群规模
        audit = self.client.get(f"/api/segment/{seg_id}/audit").json()
        self.assertGreaterEqual(audit["total"], 1)
        self.assertTrue(any(item["结果"] == "拒绝" for item in audit["items"]))
        trace = self.client.get(f"/api/tagmgr/{tag_id}/trace").json()
        self.assertIn("SEGM-0002", {s["分群编号"] for s in trace["引用分群"]})

    def test_refresh_failure_does_not_touch_tag_coverage(self) -> None:
        seg_id = self._seg_id("SEGM-EQUAL")
        tag_id = int(store.find_by_code("tagmgr", "标签编号", "TAGM-LOYAL")["id"])
        before = self.client.get(f"/api/tagmgr/{tag_id}").json()["覆盖对象数"]

        self.client.post(f"/api/segment/{seg_id}/actions", json={"values": {"action": "失效分群"}})
        self.client.post(f"/api/segment/{seg_id}/actions", json={"values": {"action": "提交规则校验"}})
        resp = self.client.post(f"/api/segment/{seg_id}/refresh")
        batch = resp.json()["entry"]["刷新批次"]
        resp = self.client.post(
            f"/api/segment/{seg_id}/refresh/complete",
            json={"values": {"batch": batch, "failed": True, "failure_reason": "引擎超时"}},
        )
        self.assertEqual(resp.json()["ok"], False)
        self.assertIn("引擎超时", resp.json()["message"])
        # 失败后两侧数字都没变
        self.assertEqual(
            self.client.get(f"/api/tagmgr/{tag_id}").json()["覆盖对象数"], before
        )
        self.assertEqual(
            self.client.get(f"/api/segment/{seg_id}").json()["最终规模"], 300
        )

    def test_illegal_transition_rejected(self) -> None:
        # 草稿分群不能直接生效，必须先提交规则校验
        resp = self.client.post(
            "/api/segment",
            json={"values": {
                "分群编号": "SEGM-DRAFT",
                "分群名称": "草稿态分群",
                "筛选规则": "x=1",
                "关联标签编号": "TAGM-LOYAL",
                "最小规模": 1,
                "最大规模": 10,
            }},
        )
        seg_id = resp.json()["entry"]["id"]
        resp = self.client.post(f"/api/segment/{seg_id}/actions", json={"values": {"action": "生效分群"}})
        self.assertEqual(resp.json()["ok"], False)
        self.assertIn("状态流转被拒绝", resp.json()["message"])


if __name__ == "__main__":
    unittest.main()
