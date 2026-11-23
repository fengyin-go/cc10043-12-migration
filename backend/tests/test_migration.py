"""载荷编码迁移子系统的单元测试（仅依赖标准库，python3 -I 可直接运行）。

覆盖：
* 旧字面量安全解析（含 True/None，拒绝调用表达式）；
* 来源+格式核验、同文异源不归并、来源不明保留原文；
* 信封版本/哈希核验与篡改检测；
* 迁移批次预备/核对/发布/撤回全生命周期；
* 回放受理水位、中断续跑、重跑幂等、回滚对账；
* 分段迁移期间接入与回放并发的读写冲突隔离（多线程压测）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.migration import (  # noqa: E402
    ENCODING_JSON_V2,
    ENCODING_PY_LITERAL_V1,
    ENCODING_UNKNOWN,
    EnvelopeError,
    LegacyParseError,
    build_envelope,
    classify_legacy,
    parse_legacy,
    parse_payload,
    verify_envelope,
)
from app.migration.service import MigrationService  # noqa: E402
from app.migration.store import MigrationStore  # noqa: E402

SRC_A = "src.order_connector_a"
SRC_B = "src.order_connector_b"
SRC_UNKNOWN = "src.rogue_writer"

LEGACY = (
    "{'order_id': 'ORD-1', 'paid': True, 'refunded': False, "
    "'coupon': None, 'items': [{'sku': 'S1', 'qty': 1}]}"
)


class CodecTests(unittest.TestCase):
    def test_legacy_literal_roundtrip(self) -> None:
        data = parse_legacy(LEGACY)
        self.assertIs(data["paid"], True)
        self.assertIsNone(data["coupon"])
        # JSON 序列化后 True/None 正确变成 true/null。
        encoded = json.dumps(data, ensure_ascii=False)
        self.assertIn("true", encoded)
        self.assertIn("null", encoded)

    def test_literal_eval_rejects_code(self) -> None:
        # 绝不允许把载荷当代码执行。
        for attack in ("__import__('os').system('echo pwn')", "open('/etc/passwd').read()",
                       "lambda: 1", "1 + 1"):
            with self.assertRaises(LegacyParseError):
                parse_legacy(attack)

    def test_same_text_different_source_not_merged(self) -> None:
        verdict_a = classify_legacy(SRC_A, LEGACY)
        verdict_b = classify_legacy(SRC_B, LEGACY)
        self.assertTrue(verdict_a["convertible"])
        self.assertTrue(verdict_b["convertible"])
        self.assertNotEqual(
            verdict_a["classification_key"], verdict_b["classification_key"]
        )

    def test_unknown_source_keeps_raw(self) -> None:
        verdict = classify_legacy(SRC_UNKNOWN, LEGACY)
        self.assertFalse(verdict["convertible"])
        self.assertEqual(verdict["encoding"], ENCODING_UNKNOWN)
        self.assertIn("无法转换", verdict["reason"])

    def test_marker_mismatch_keeps_raw(self) -> None:
        # 来源登记的是 Python 字面量格式，但文本是纯 JSON（无该格式特征）。
        verdict = classify_legacy(SRC_A, '{"paid": true}')
        self.assertFalse(verdict["convertible"])
        self.assertEqual(verdict["encoding"], ENCODING_UNKNOWN)

    def test_envelope_verify_and_tamper_detection(self) -> None:
        envelope = build_envelope(source=SRC_A, message_id="m1", data=parse_legacy(LEGACY),
                                  origin_format=ENCODING_PY_LITERAL_V1, migrated_from=LEGACY)
        summary = verify_envelope(envelope)
        self.assertEqual(summary["enc"], ENCODING_JSON_V2)
        self.assertTrue(summary["has_legacy_text"])

        tampered = json.loads(json.dumps(envelope))
        tampered["data"]["paid"] = False
        with self.assertRaises(EnvelopeError):
            verify_envelope(tampered)

        bad_version = json.loads(json.dumps(envelope))
        bad_version["enc"] = "json.envelope.v999"
        with self.assertRaises(EnvelopeError):
            verify_envelope(bad_version)

    def test_parse_payload_distinguishes_encodings(self) -> None:
        envelope = build_envelope(source=SRC_A, message_id="m1", data={"a": 1})
        enc, parsed = parse_payload(json.dumps(envelope))
        self.assertEqual(enc, ENCODING_JSON_V2)

        enc, parsed = parse_payload(LEGACY)
        self.assertEqual(enc, ENCODING_PY_LITERAL_V1)
        self.assertIs(parsed["paid"], True)

        enc, parsed = parse_payload("not-anything-known")
        self.assertEqual(enc, ENCODING_UNKNOWN)
        self.assertEqual(parsed, "not-anything-known")


class MigrationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = MigrationService(MigrationStore())

    def test_prepare_publish_withdraw_cycle(self) -> None:
        self.svc.load_legacy(source=SRC_A, message_id="M1", raw_text=LEGACY)
        prep = self.svc.prepare_migration(source=SRC_A)
        self.assertTrue(prep["ok"])
        self.assertEqual(prep["convertible"], 1)

        report = self.svc.verify_before_publish(prep["batch_id"])
        self.assertTrue(report["ok"])

        # 发布前回放仍读 v1。
        self.assertEqual(self.svc.stored_versions(SRC_A)[0]["enc"], ENCODING_PY_LITERAL_V1)
        self.assertTrue(self.svc.publish_batch(prep["batch_id"])["ok"])
        self.assertEqual(self.svc.stored_versions(SRC_A)[0]["enc"], ENCODING_JSON_V2)

        withdrawn = self.svc.withdraw_batch(prep["batch_id"], reason="演练撤回")
        self.assertTrue(withdrawn["ok"])
        self.assertEqual(self.svc.stored_versions(SRC_A)[0]["enc"], ENCODING_PY_LITERAL_V1)
        # 撤回后不能重复撤回。
        again = self.svc.withdraw_batch(prep["batch_id"], reason="再次撤回")
        self.assertFalse(again["ok"])

    def test_unconvertible_item_preserved_in_detail(self) -> None:
        self.svc.load_legacy(source=SRC_UNKNOWN, message_id="U1", raw_text="?? unknown bytes")
        prep = self.svc.prepare_migration(source=SRC_UNKNOWN)
        self.assertEqual(prep["unconvertible"], 1)
        item = prep["items"][0]
        self.assertIsNone(item["new_version_id"])
        self.assertEqual(item["new_enc"], ENCODING_UNKNOWN)
        self.assertEqual(item["raw_text"], "?? unknown bytes")

    def test_single_active_batch_per_source(self) -> None:
        self.svc.load_legacy(source=SRC_A, message_id="M1", raw_text=LEGACY)
        first = self.svc.prepare_migration(source=SRC_A)
        second = self.svc.prepare_migration(source=SRC_A)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])

    def test_segmented_migration_skips_already_migrated_messages(self) -> None:
        for index in range(4):
            self.svc.load_legacy(
                source=SRC_A, message_id=f"M{index}", raw_text=LEGACY.replace("ORD-1", f"ORD-{index}")
            )
        first = self.svc.prepare_migration(
            source=SRC_A, message_ids=["M0", "M1"], scope_note="第一段"
        )
        self.assertTrue(self.svc.publish_batch(first["batch_id"])["ok"])

        # 第二段不带消息过滤，应只挑出尚未迁移的 M2/M3，不重复 M0/M1。
        second = self.svc.prepare_migration(source=SRC_A, scope_note="第二段")
        self.assertTrue(second["ok"])
        self.assertEqual(
            sorted(item["message_id"] for item in second["items"]), ["M2", "M3"]
        )
        self.assertTrue(self.svc.publish_batch(second["batch_id"])["ok"])

        # 全部迁移完成后再预备，应明确返回无可迁移载荷。
        third = self.svc.prepare_migration(source=SRC_A)
        self.assertFalse(third["ok"])

    def test_ingress_rejects_unknown_source_and_bad_envelope(self) -> None:
        good = build_envelope(source=SRC_A, message_id="N1", data={"x": 1})
        ok = self.svc.ingress_json(source=SRC_A, raw_json_text=json.dumps(good))
        self.assertTrue(ok["ok"])

        bad_source = self.svc.ingress_json(source=SRC_UNKNOWN, raw_json_text=json.dumps(good))
        self.assertFalse(bad_source["ok"])

        mismatched = build_envelope(source=SRC_B, message_id="N2", data={"x": 2})
        bad_match = self.svc.ingress_json(source=SRC_A, raw_json_text=json.dumps(mismatched))
        self.assertFalse(bad_match["ok"])

        bad_enc = self.svc.ingress_json(source=SRC_A, raw_json_text=LEGACY)
        self.assertFalse(bad_enc["ok"])


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = MigrationService(MigrationStore())
        self.svc.load_legacy(source=SRC_A, message_id="M1", raw_text=LEGACY)
        self.svc.load_legacy(source=SRC_A, message_id="M2", raw_text=LEGACY.replace("ORD-1", "ORD-2"))
        prep = self.svc.prepare_migration(source=SRC_A)
        self.assertTrue(self.svc.publish_batch(prep["batch_id"])["ok"])

    def test_interrupt_resume_and_rerun_idempotent(self) -> None:
        opened = self.svc.open_replay(source=SRC_A, target_topic="t1")
        rid = opened["request_id"]
        first = self.svc.run_replay(rid, limit=1)
        self.assertEqual(first["delivered_this_run"], 1)
        self.svc.interrupt_replay(rid, reason="演练中断")

        resumed = self.svc.run_replay(rid)
        self.assertEqual(resumed["status"], "已完成")
        self.assertEqual(resumed["delivered_this_run"], 1)

        # 重跑不产生重复交付。
        rerun = self.svc.run_replay(rid)
        self.assertEqual(rerun["delivered_this_run"], 0)
        self.assertEqual(len(self.svc.store.list_ledger(rid)), 2)

    def test_cutoff_isolates_late_ingress(self) -> None:
        opened = self.svc.open_replay(source=SRC_A, target_topic="t1")
        rid = opened["request_id"]
        cutoff = opened["cutoff_seq"]

        envelope = build_envelope(source=SRC_A, message_id="LATE", data={"late": True})
        ingested = self.svc.ingress_json(source=SRC_A, raw_json_text=json.dumps(envelope))
        self.assertGreater(ingested["seq"], cutoff)

        result = self.svc.run_replay(rid)
        delivered = {row["message_id"] for row in result["delivered"]}
        self.assertNotIn("LATE", delivered)
        # 新请求（水位更新后）可见 LATE。
        opened2 = self.svc.open_replay(source=SRC_A, target_topic="t2")
        result2 = self.svc.run_replay(opened2["request_id"])
        self.assertIn("LATE", {row["message_id"] for row in result2["delivered"]})

    def test_reconcile_flags_withdraw_drift_but_not_scope_expansion(self) -> None:
        opened = self.svc.open_replay(source=SRC_A, target_topic="t1")
        rid = opened["request_id"]
        self.svc.run_replay(rid)
        pre = self.svc.reconcile(rid)
        self.assertTrue(pre["ok"])

        # 撤回迁移批次，已交付的 v2 与当前可见的 v1 出现漂移。
        batch_id = self.svc.store.list_batches(SRC_A)[0]["batch_id"]
        self.assertTrue(self.svc.withdraw_batch(batch_id, reason="回滚演练")["ok"])
        post = self.svc.reconcile(rid)
        self.assertFalse(post["ok"])
        self.assertTrue(any("漂移" in problem for problem in post["problems"]))
        self.assertLessEqual(post["confirmed_seq"], post["cutoff_seq"])

    def test_replay_reads_either_encoding(self) -> None:
        # B 未迁移，回放读 v1；A 已迁移读 v2。
        self.svc.load_legacy(source=SRC_B, message_id="B1", raw_text=LEGACY)
        rb = self.svc.open_replay(source=SRC_B, target_topic="tb")
        result_b = self.svc.run_replay(rb["request_id"])
        self.assertEqual(result_b["delivered"][0]["read_enc"], ENCODING_PY_LITERAL_V1)

        ra = self.svc.open_replay(source=SRC_A, target_topic="ta")
        result_a = self.svc.run_replay(ra["request_id"])
        self.assertTrue(all(
            row["read_enc"] == ENCODING_JSON_V2 for row in result_a["delivered"]
        ))


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_ingress_during_replay_no_corruption(self) -> None:
        """分段迁移中间态：回放读一批未迁完的数据，接入同时往同来源写新版 JSON。"""
        store = MigrationStore()
        svc = MigrationService(store)
        for index in range(20):
            svc.load_legacy(source=SRC_A, message_id=f"OLD-{index}", raw_text=LEGACY)
        # 只迁移前 10 条并发布，模拟「迁了一半」。
        prep = svc.prepare_migration(
            source=SRC_A, message_ids=[f"OLD-{i}" for i in range(10)]
        )
        self.assertTrue(svc.publish_batch(prep["batch_id"])["ok"])

        opened = svc.open_replay(source=SRC_A, target_topic="t-concurrent")
        rid = opened["request_id"]

        errors: list[Exception] = []

        def writer() -> None:
            try:
                for index in range(20):
                    envelope = build_envelope(
                        source=SRC_A, message_id=f"NEW-{index}", data={"i": index}
                    )
                    result = svc.ingress_json(
                        source=SRC_A, raw_json_text=json.dumps(envelope)
                    )
                    if not result["ok"]:
                        raise RuntimeError(result["message"])
            except Exception as exc:  # noqa: BLE001 - 测试线程需收集所有异常
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        # 与写入并发执行回放（旧水位，NEW-* 全部不可见）。
        replay_result = svc.run_replay(rid)
        thread.join()

        self.assertEqual(errors, [])
        delivered = {row["message_id"] for row in replay_result["delivered"]}
        self.assertEqual(delivered, {f"OLD-{i}" for i in range(20)})
        self.assertTrue(all(not msg.startswith("NEW-") for msg in delivered))
        # 前 10 条读到 v2，未迁的 10 条读到 v1。
        enc_by_msg = {row["message_id"]: row["read_enc"] for row in replay_result["delivered"]}
        self.assertTrue(all(enc_by_msg[f"OLD-{i}"] == ENCODING_JSON_V2 for i in range(10)))
        self.assertTrue(all(enc_by_msg[f"OLD-{i}"] == ENCODING_PY_LITERAL_V1 for i in range(10, 20)))
        # 台账无重复、无越界序号。
        ledger = store.list_ledger(rid)
        self.assertEqual(len(ledger), 20)
        self.assertTrue(all(row["origin_seq"] <= opened["cutoff_seq"] for row in ledger))


if __name__ == "__main__":
    unittest.main(verbosity=2)
