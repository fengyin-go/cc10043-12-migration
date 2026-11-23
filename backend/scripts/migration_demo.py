#!/usr/bin/env python3
"""载荷编码迁移与版本化回放：端到端场景演示。

剧情严格对应需求中的验收场景：

1. 两条内容**完全相同**、但接入来源依据不同（连接器 A / B）的旧字面量载荷，
   验证迁移明细按 (来源, 原写入格式) 分开核验，不会被归并；
2. 再放一条来源不明载荷，验证保留原文并说明无法转换；
3. A 来源开始回放、B 尚未迁移时制造中断，并在中断期间新增一条新版 JSON
   记录 C，验证读写不冲突；
4. 恢复旧回放任务：交付仍按受理水位读到旧数据，C 对旧请求不可见；
5. 新批次查询能看到 C 的版本与数据；完成 A 迁移并发布后，新回放读取 v2；
6. 撤回批次发布，验证回滚后旧请求重新回落到 v1 原载荷，对账一致。

运行（在 backend/ 目录下）：
    python3 -I scripts/migration_demo.py
"""
from __future__ import annotations

import json
import os
import sys

# 允许以 `python3 scripts/migration_demo.py` 直接运行：把 backend 根目录放进 sys.path。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.migration import (  # noqa: E402
    ENCODING_JSON_V2,
    ENCODING_PY_LITERAL_V1,
    ENCODING_UNKNOWN,
    get_migration_service,
    reset_migration_store,
)
from app.migration.registry import describe_origin  # noqa: E402

SRC_A = "src.order_connector_a"
SRC_B = "src.order_connector_b"
SRC_UNKNOWN = "src.rogue_writer"

# 历史订单载荷：连接器直写的 Python 字面量，保留 True / None。
LEGACY_TEXT_A = (
    "{'order_id': 'ORD-1001', 'paid': True, 'refunded': False, "
    "'coupon': None, 'items': [{'sku': 'SKU-1', 'qty': 2}]}"
)
# 与 A 逐字相同的文本，但来源依据是连接器 B —— 不能归并为同一类。
LEGACY_TEXT_B = LEGACY_TEXT_A
LEGACY_TEXT_UNKNOWN = "raw-bytes-from-unknown-writer: ?? not a literal"


def banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def show(label: str, value: object) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✔ {message}")


def main() -> None:
    reset_migration_store()
    svc = get_migration_service()

    banner("步骤 0：来源登记表（迁移依据：接入来源 + 原写入格式）")
    show("已登记来源", [describe_origin(SRC_A), describe_origin(SRC_B),
                       describe_origin(SRC_UNKNOWN)])

    banner("步骤 1：装载存量载荷——A/B 文本逐字相同，来源不明载荷原样保留")
    a_legacy = svc.load_legacy(source=SRC_A, message_id="MSG-A1", raw_text=LEGACY_TEXT_A)
    b_legacy = svc.load_legacy(source=SRC_B, message_id="MSG-B1", raw_text=LEGACY_TEXT_B)
    u_legacy = svc.load_legacy(source=SRC_UNKNOWN, message_id="MSG-U1",
                               raw_text=LEGACY_TEXT_UNKNOWN)
    assert_true(a_legacy["classification"]["convertible"], "A 来源字面量核验可转换")
    assert_true(b_legacy["classification"]["convertible"], "B 来源字面量核验可转换")
    assert_true(not u_legacy["classification"]["convertible"], "来源不明载荷标记为不可转换")
    assert_true(
        a_legacy["classification"]["classification_key"]
        != b_legacy["classification"]["classification_key"],
        "文本相同但来源不同，分类键不同，不会被归并为同一类",
    )
    assert_true(
        u_legacy["raw_text"] == LEGACY_TEXT_UNKNOWN,
        "来源不明载荷原文逐字保留",
    )

    banner("步骤 2：分别为 A、B 预备迁移批次，核对迁移明细")
    prep_a = svc.prepare_migration(source=SRC_A, scope_note="A 来源第一批分段迁移")
    prep_b = svc.prepare_migration(source=SRC_B, scope_note="B 来源第一批分段迁移")
    show("A 迁移批次明细", prep_a)
    show("B 迁移批次明细", prep_b)
    assert_true(prep_a["ok"] and prep_b["ok"], "两个来源的迁移批次均预备成功")
    item_a = prep_a["items"][0]
    item_b = prep_b["items"][0]
    assert_true(item_a["classification_key"] != item_b["classification_key"],
                "迁移明细保留各自来源依据，同文不同源仍是两条明细")
    assert_true(item_a["new_enc"] == ENCODING_JSON_V2, "A 新版本为带版本标识的 JSON")
    # 新版本此刻未发布：回放视图里 A/B 仍应读到 v1 原载荷。
    view_before_publish = svc.stored_versions(SRC_A)
    assert_true(
        all(row["enc"] == ENCODING_PY_LITERAL_V1 for row in view_before_publish),
        "未发布迁移版本对回放不可见，A 仍读取原载荷",
    )

    banner("步骤 3：批次核对 → 发布 → 切换前核对存量与示例数据（失败可撤回）")
    verify_report = svc.verify_before_publish(prep_a["batch_id"])
    show("A 批次发布前核对", verify_report)
    assert_true(verify_report["ok"], "A 批次版本、哈希与原文核对通过")
    publish_a = svc.publish_batch(prep_a["batch_id"])
    show("发布 A 批次", publish_a)
    assert_true(publish_a["ok"], "A 批次发布成功")
    check = svc.pre_cutover_check(source=SRC_A, sample_message_ids=["MSG-A1"])
    show("A 切换前核对（存量 + 示例）", check)
    assert_true(check["ok"], "A 存量与示例数据核对通过，允许切换接入与回放入口")
    view_a = svc.stored_versions(SRC_A)
    assert_true(
        view_a[0]["enc"] == ENCODING_JSON_V2 and view_a[0]["published"],
        "发布后 A 的可见版本切换为 v2 JSON",
    )
    view_b = svc.stored_versions(SRC_B)
    assert_true(
        all(row["enc"] == ENCODING_PY_LITERAL_V1 for row in view_b),
        "B 尚未发布，仍停留在 v1 原载荷（分段迁移中间态）",
    )

    banner("步骤 4：受理 A 的回放请求，冻结水位后只交付一条即中断")
    opened = svc.open_replay(source=SRC_A, target_topic="topic.order.replay")
    request_id = opened["request_id"]
    cutoff = opened["cutoff_seq"]
    show("受理回放（冻结水位）", opened)
    first_run = svc.run_replay(request_id, limit=1)
    show("首跑（模拟中断点）", first_run)
    interrupted = svc.interrupt_replay(request_id, reason="模拟分段迁移期间的回放中断")
    show("中断", interrupted)

    banner("步骤 5：中断期间新接入写入新版 JSON 记录 C（读写并发，不覆盖旧版本）")
    envelope_c = svc.build_ingress_envelope(
        source=SRC_A,
        message_id="MSG-C1",
        data={"order_id": "ORD-2002", "paid": False, "coupon": None, "note": "新接入"},
    )
    ingest_c = svc.ingress_json(source=SRC_A, raw_json_text=json.dumps(envelope_c, ensure_ascii=False))
    show("接入 C", ingest_c)
    assert_true(ingest_c["ok"], "C 以版本化 JSON 接入成功")
    assert_true(ingest_c["seq"] > cutoff, "C 的写入序号晚于回放受理水位")

    # 来源不明的写入必须在入口被拒绝。
    bad = svc.ingress_json(source=SRC_UNKNOWN, raw_json_text=json.dumps(envelope_c))
    assert_true(not bad["ok"], "未登记来源的新接入被入口拒绝")

    banner("步骤 6：恢复旧回放任务——仍按受理水位交付，C 不可见、确认范围不扩大")
    resumed = svc.resume_replay(request_id)
    show("恢复回放", resumed)
    delivered_ids = {row["message_id"] for row in svc.store.list_ledger(request_id)}
    assert_true("MSG-A1" in delivered_ids, "旧任务恢复后完成 MSG-A1 交付")
    assert_true("MSG-C1" not in delivered_ids, "C 对旧回放请求不可见，旧确认范围未被新版输入扩大")
    a_delivery = next(row for row in resumed["delivered"] if row["message_id"] == "MSG-A1") \
        if resumed["delivered"] else None
    # MSG-A1 在首跑已交付，恢复时台账去重；查台账确认它读的是 v2。
    ledger_a = next(row for row in svc.store.list_ledger(request_id) if row["message_id"] == "MSG-A1")
    assert_true(ledger_a["enc"] == ENCODING_JSON_V2, "A 已迁移，旧任务实际交付的是 v2 版本")
    assert_true(ledger_a["origin_seq"] <= cutoff, "所有交付序号均不越过受理水位")

    banner("步骤 7：新批次查询——能查到 C 的版本与数据")
    current_view = svc.stored_versions(SRC_A)
    show("A 来源当前可见版本", [
        {k: row[k] for k in ("message_id", "enc", "version_no", "published", "seq")}
        for row in current_view
    ])
    c_row = next(row for row in current_view if row["message_id"] == "MSG-C1")
    assert_true(c_row["enc"] == ENCODING_JSON_V2, "新查询可见 C 的 v2 版本")
    assert_true(c_row["envelope"]["data"]["order_id"] == "ORD-2002", "C 的数据可读取且内容正确")

    banner("步骤 8：重跑幂等性 + 回滚（撤回 A 批次）后对账")
    rerun = svc.run_replay(request_id)
    show("重跑旧请求", rerun)
    assert_true(rerun["delivered_this_run"] == 0, "重跑不产生重复交付（台账按消息身份去重）")
    reconcile_before = svc.reconcile(request_id)
    assert_true(reconcile_before["ok"], "撤回前对账无异常")

    withdraw = svc.withdraw_batch(prep_a["batch_id"], reason="切换后发现异常，撤回当前批次")
    show("撤回 A 批次", withdraw)
    rolled_back_view = svc.stored_versions(SRC_A)
    a_after = next(row for row in rolled_back_view if row["message_id"] == "MSG-A1")
    assert_true(a_after["enc"] == ENCODING_PY_LITERAL_V1,
                "撤回后 A 回落到仍可读的 v1 原载荷")
    c_after = next(row for row in rolled_back_view if row["message_id"] == "MSG-C1")
    assert_true(c_after["enc"] == ENCODING_JSON_V2, "C 是新接入数据，不受批次撤回影响")

    reconcile_after = svc.reconcile(request_id)
    show("撤回后对账", reconcile_after)
    assert_true(
        all(problem.startswith("消息 MSG-A1") for problem in reconcile_after["problems"])
        and len(reconcile_after["problems"]) == 1,
        "对账如实指出 MSG-A1 当前可见版本已回退，但确认范围仍未越过水位",
    )
    assert_true(reconcile_after["confirmed_seq"] <= cutoff, "确认序号不超过受理水位")

    # 撤回后新受理的回放读取 v1 原载荷，证明回放入口两种编码都能读。
    opened2 = svc.open_replay(source=SRC_A, target_topic="topic.order.replay.round2")
    run2 = svc.run_replay(opened2["request_id"])
    encs = {row["message_id"]: row["read_enc"] for row in run2["delivered"]}
    show("撤回后新回放的读取编码", encs)
    assert_true(encs["MSG-A1"] == ENCODING_PY_LITERAL_V1, "新回放对 A 读取 v1 原载荷")
    assert_true(encs["MSG-C1"] == ENCODING_JSON_V2, "新回放对 C 读取 v2 JSON")

    banner("步骤 9：B 来源全程未迁移，旧回放始终读取原载荷")
    opened_b = svc.open_replay(source=SRC_B, target_topic="topic.order.replay.b")
    run_b = svc.run_replay(opened_b["request_id"])
    show("B 回放结果", run_b["delivered"])
    assert_true(
        all(row["read_enc"] == ENCODING_PY_LITERAL_V1 for row in run_b["delivered"]),
        "未迁移来源 B 的回放仍可读原载荷",
    )

    banner("全部断言通过：迁移、冲突隔离、回放、重跑与回滚对账均符合预期")


if __name__ == "__main__":
    main()
