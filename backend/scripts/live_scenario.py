#!/usr/bin/env python3
"""对运行中的 uvicorn 服务跑真实 HTTP 场景（仅标准库）。

默认地址 http://127.0.0.1:8017，可用环境变量 BASE 覆盖。
进程内状态互相独立，本脚本只验证 HTTP 契约与剧情隔离。
"""
from __future__ import annotations

import json
import os
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8017")
A = "src.order_connector_a"
B = "src.order_connector_b"
LEGACY = ("{'order_id': 'ORD-1001', 'paid': True, 'refunded': False, "
          "'coupon': None, 'items': [{'sku': 'SKU-1', 'qty': 2}]}")


def call(method: str, path: str, payload: object = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ✔ {msg}")


def main() -> None:
    suffix = os.urandom(4).hex()
    msg_a, msg_b = f"LIVE-A-{suffix}", f"LIVE-B-{suffix}"

    print("== 同文异源装载，核验分类键不同 ==")
    ra = call("POST", "/api/migration/legacy/load",
              {"source": A, "message_id": msg_a, "raw_text": LEGACY})
    rb = call("POST", "/api/migration/legacy/load",
              {"source": B, "message_id": msg_b, "raw_text": LEGACY})
    check(ra["raw_text"] == rb["raw_text"] == LEGACY, "两条载荷原文逐字相同")
    check(ra["classification"]["classification_key"]
          != rb["classification"]["classification_key"],
          "来源依据不同，分类键不同，不归并")

    print("== 预备并发布 A，B 暂不迁移 ==")
    pa = call("POST", "/api/migration/prepare",
              {"source": A, "message_ids": [msg_a], "scope_note": "live"})
    pb = call("POST", "/api/migration/prepare",
              {"source": B, "message_ids": [msg_b], "scope_note": "live"})
    check(pa["items"][0]["classification_key"] != pb["items"][0]["classification_key"],
          "迁移明细同样保留双来源依据")
    check(call("POST", f"/api/migration/batches/{pa['batch_id']}/publish")["ok"],
          "A 批次发布成功")

    print("== 受理回放后中断，期间插入新版 JSON 记录 C ==")
    opened = call("POST", "/api/replay/open",
                  {"source": A, "target_topic": "topic.live"})
    rid, cutoff = opened["request_id"], opened["cutoff_seq"]
    call("POST", f"/api/replay/{rid}/interrupt", {"reason": "live 中断演练"})
    envelope = {
        "enc": "json.envelope.v2", "source": A,
        "message_id": f"LIVE-C-{suffix}",
        "data": {"order_id": "ORD-2002", "paid": False, "coupon": None},
    }
    # 手工补哈希，避免依赖后端构造器（也可直接调装载接口外的 build 逻辑）。
    import hashlib
    canonical = json.dumps(envelope["data"], ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    envelope["data_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    ingested = call("POST", "/api/ingress/json",
                    {"source": A, "raw_json_text": json.dumps(envelope, ensure_ascii=False)})
    check(ingested["ok"] and ingested["seq"] > cutoff, "C 接入成功且序号晚于受理水位")

    print("== 恢复旧任务：C 不可见，确认范围不扩大 ==")
    resumed = call("POST", f"/api/replay/{rid}/resume")
    delivered = {d["message_id"]: d for d in resumed["delivered"]}
    check(msg_a in delivered and f"LIVE-C-{suffix}" not in delivered,
          "旧任务只交付受理范围内消息，C 被水位隔离")
    check(delivered[msg_a]["read_enc"] == "json.envelope.v2",
          "A 已迁移，实际交付版本为 v2 JSON")

    print("== 新批次查询可见 C ==")
    versions = call("GET", f"/api/migration/versions?source={A}")["items"]
    c_row = next(v for v in versions if v["message_id"] == f"LIVE-C-{suffix}")
    check(c_row["enc"] == "json.envelope.v2"
          and c_row["envelope"]["data"]["order_id"] == "ORD-2002",
          "新查询能拿到 C 的版本与数据")

    print("== 撤回 A 批次后对账标记漂移，确认序号不越界 ==")
    check(call("POST", f"/api/migration/batches/{pa['batch_id']}/withdraw",
               {"reason": "live 回滚"})["ok"], "批次撤回成功")
    recon = call("GET", f"/api/replay/{rid}/reconcile")
    check(any("漂移" in p for p in recon["problems"]), "对账如实标记版本回退漂移")
    check(recon["confirmed_seq"] <= cutoff, "确认范围未越过受理水位")

    print("== B 未迁移，回放仍读 v1 原载荷 ==")
    rb_open = call("POST", "/api/replay/open",
                   {"source": B, "target_topic": "topic.live.b"})
    rb_run = call("POST", f"/api/replay/{rb_open['request_id']}/run", {})
    check(all(d["read_enc"] == "python_literal_repr_v1" for d in rb_run["delivered"]),
          "未迁移来源 B 回放入口仍可读原载荷")

    print("\n真实 HTTP 场景全部断言通过")


if __name__ == "__main__":
    main()
