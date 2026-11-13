"""纯 HTTP 端到端冒烟：异常样例修复、并发刷新、换版传播、审计追溯。

用法：先启动 uvicorn，再 python -I scripts/e2e_http_check.py
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def find(module: str, code: str) -> int:
    return call("GET", f"/api/{module}?keyword={code}")["items"][0]["id"]


def main() -> None:
    seg_id = find("segment", "SEGM-0002")
    tag_id = find("tagmgr", "TAGM-NIGHT")
    dimp_id = find("dimprop", "DIMP-0004")

    # 0. 初始异常样例：上下限写反且标签草稿，生效必须被拒并落拒绝审计
    r = call("POST", f"/api/segment/{seg_id}/actions", {"values": {"action": "生效分群"}})
    assert not r["ok"], r
    assert "最小规模 500 大于最大规模 50" in r["message"] and "标签未发布" in r["message"]
    print("异常样例拒绝 ->", r["message"])

    # 1. 修正上下限为 50~500
    r = call("PUT", f"/api/segment/{seg_id}", {"values": {"最小规模": 50, "最大规模": 500}})
    assert r["ok"], r

    # 2. 属性走 草稿 -> 属性评审中 -> 已发布
    assert call("POST", f"/api/dimprop/{dimp_id}/actions", {"values": {"action": "提交属性评审"}})["ok"]
    assert call("POST", f"/api/dimprop/{dimp_id}/actions", {"values": {"action": "发布属性"}})["ok"]

    # 3. 标签评审 -> 发布 v1
    assert call("POST", f"/api/tagmgr/{tag_id}/actions", {"values": {"action": "提交逻辑评审"}})["ok"]
    r = call("POST", f"/api/tagmgr/{tag_id}/actions", {"values": {"action": "发布标签"}})
    assert r["ok"] and r["entry"]["当前版本"] == 1, r

    # 4. 发起刷新
    r = call("POST", f"/api/segment/{seg_id}/refresh")
    assert r["ok"], r
    batch = r["entry"]["刷新批次"]

    # 5. 并发回填同一批次：首份完整结果胜出
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def complete(count: int) -> None:
        barrier.wait()
        results.append(call(
            "POST", f"/api/segment/{seg_id}/refresh/complete",
            {"values": {"batch": batch, "coverage": count}},
        ))

    threading.Thread(target=complete, args=(260,)).start()
    threading.Thread(target=complete, args=(480,)).start()
    time.sleep(0.5)
    oks = [x for x in results if x["ok"]]
    fails = [x for x in results if not x["ok"]]
    assert len(oks) == 1 and len(fails) == 1, results
    winning_count = oks[0]["entry"]["最终规模"]
    print("并发刷新：首份完整结果胜出，重复结果被拒 ->", fails[0]["message"])

    # 6. 生效
    r = call("POST", f"/api/segment/{seg_id}/actions", {"values": {"action": "生效分群"}})
    assert r["ok"], r
    print("生效成功 ->", r["message"])

    # 7. 分群最终规模与标签覆盖对象数严格一致
    seg = call("GET", f"/api/segment/{seg_id}")
    tag = call("GET", f"/api/tagmgr/{tag_id}")
    assert seg["最终规模"] == tag["覆盖对象数"] == winning_count
    print(f"数字一致：最终规模={seg['最终规模']}，覆盖对象数={tag['覆盖对象数']}，引用版本=v{seg['标签版本']}")

    # 8. 审计与追溯
    trace = call("GET", f"/api/tagmgr/{tag_id}/trace")
    audit = call("GET", f"/api/segment/{seg_id}/audit")
    assert any(i["结果"] == "拒绝" for i in audit["items"])
    print("追溯引用分群 ->", [s["分群编号"] for s in trace["引用分群"]],
          "| 审计含拒绝记录，共", audit["total"], "条")

    # 9. 标签换版：已生效分群打回规则校验中，旧规模留存
    assert call("PUT", f"/api/tagmgr/{tag_id}", {"values": {"计算逻辑": "夜间活跃口径v2"}})["ok"]
    call("POST", f"/api/tagmgr/{tag_id}/actions", {"values": {"action": "提交逻辑评审"}})
    r = call("POST", f"/api/tagmgr/{tag_id}/actions", {"values": {"action": "发布标签"}})
    assert r["entry"]["当前版本"] == 2
    seg = call("GET", f"/api/segment/{seg_id}")
    assert seg["status"] == "规则校验中" and seg["最终规模"] == winning_count
    print(f"换版传播：分群回到{seg['status']}，旧最终规模 {seg['最终规模']} 留存，"
          f"历史规模 {len(seg['历史规模列表'])} 条，仍引用 v{seg['标签版本']}")

    # 10. 换版后不刷新直接生效必须被拒
    r = call("POST", f"/api/segment/{seg_id}/actions", {"values": {"action": "生效分群"}})
    assert not r["ok"] and "版本不一致" in r["message"], r
    print("版本不一致拒绝 ->", r["message"])

    print("ALL HTTP CHECKS PASSED")


if __name__ == "__main__":
    main()
