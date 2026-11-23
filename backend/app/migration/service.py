"""迁移与回放编排服务：把编码核验、版本仓和回放流程串成两条入口。

入口一：接入（ingress）
    新接入来源只能写通过版本/哈希核验的 ``json.envelope.v2``；来源未登记
    直接拒绝。迁移与接入可以并发，版本仓的追加式写入保证互不覆盖。

入口二：回放（replay）
    请求受理时冻结 ``cutoff_seq`` 水位，回放只读取该水位下已发布的每消息
    最新版本——可能是迁移后的 v2 JSON，也可能是仍可读的 v1 原载荷。
    交付台账按消息身份去重，中断后续跑、重跑、回滚后对账都以台账为准，
    旧请求的确认范围不会因后续新写入而扩大。

迁移批次
    预备（生成未发布 v2，逐条留下来源/格式核验明细）→ 切换前核对 →
    发布（翻转发布位，失败可撤回）。撤回后旧请求重新回落到 v1 原载荷。
"""
from __future__ import annotations

import json
import threading
from typing import Any

from app.migration.codec import (
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
from app.migration.registry import describe_origin, ingest_origin
from app.migration.store import MigrationStore, get_migration_store


class MigrationService:
    """面向路由与演示脚本的应用服务。"""

    def __init__(self, store: MigrationStore | None = None) -> None:
        self.store = store or get_migration_store()
        # 来源级迁移锁：同一来源的迁移批次串行，分段迁移期间不与同来源的
        # 另一个迁移批次互相穿插；接入写新消息走追加路径，不修改旧版本。
        self._source_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _source_lock(self, source: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._source_locks.get(source)
            if lock is None:
                lock = threading.RLock()
                self._source_locks[source] = lock
            return lock

    # ------------------------------------------------------------------
    # 存量装载（把连接器历史写入的载荷登记进版本仓）
    # ------------------------------------------------------------------

    def load_legacy(self, *, source: str, message_id: str, raw_text: str) -> dict[str, Any]:
        """装载一条存量载荷；来源不明也允许装载，但原样保留并打标。"""
        verdict = classify_legacy(source, raw_text)
        row = self.store.load_legacy_payload(
            source=source, message_id=message_id, raw_text=raw_text
        )
        row["classification"] = verdict
        return row

    # ------------------------------------------------------------------
    # 入口一：新接入（只接受版本化 JSON）
    # ------------------------------------------------------------------

    def ingress_json(self, *, source: str, raw_json_text: str) -> dict[str, Any]:
        """新接收入口：核验来源与信封后追加一条已发布 v2 版本。"""
        allowed, source_message = ingest_origin(source)
        if not allowed:
            return {"ok": False, "stage": "source-check", "message": source_message}
        try:
            enc, parsed = parse_payload(raw_json_text)
        except EnvelopeError as exc:
            return {"ok": False, "stage": "envelope-verify", "message": str(exc)}
        if enc != ENCODING_JSON_V2:
            return {
                "ok": False,
                "stage": "encoding",
                "message": f"新接入只允许 {ENCODING_JSON_V2}，实际识别为 {enc}",
            }
        if parsed["source"] != source:
            return {
                "ok": False,
                "stage": "source-check",
                "message": "信封内 source 与接入来源不一致，拒绝写入",
            }
        row = self.store.append_ingest_envelope(source=source, envelope=parsed)
        return {
            "ok": True,
            "stage": "ingested",
            "message": source_message,
            "version_id": row["version_id"],
            "seq": row["seq"],
            "message_id": row["message_id"],
            "enc": row["enc"],
        }

    def build_ingress_envelope(
        self, *, source: str, message_id: str, data: Any
    ) -> dict[str, Any]:
        """供连接器改造后构造新版载荷（新接入不携带 origin_format）。"""
        return build_envelope(source=source, message_id=message_id, data=data)

    # ------------------------------------------------------------------
    # 迁移批次：预备 → 核对 → 发布 / 撤回
    # ------------------------------------------------------------------

    def prepare_migration(self, *, source: str, message_ids: list[str] | None = None,
                          scope_note: str = "") -> dict[str, Any]:
        """为一个来源的一批存量载荷预备迁移（生成未发布 v2 版本与明细）。"""
        lock = self._source_lock(source)
        with lock:
            existing = self.store.active_batch_id(source)
            if existing is not None:
                return {
                    "ok": False,
                    "message": f"来源 {source} 已有待发布批次 #{existing}，请先发布或撤回",
                }
            wanted = set(message_ids) if message_ids else None
            # 已经产生过 v2 版本的消息（无论发布还是撤回）都不再重复迁移，
            # 保证分段迁移的后续批次只覆盖尚未迁移的存量载荷。
            migrated_ids = {
                row["message_id"]
                for row in self.store.payload_versions
                if row["source"] == source and row["enc"] == ENCODING_JSON_V2
            }
            legacy_rows = [
                row
                for row in self.store.list_legacy_versions(source)
                if (wanted is None or row["message_id"] in wanted)
                and row["message_id"] not in migrated_ids
            ]
            missing = sorted(wanted - {row["message_id"] for row in legacy_rows}) if wanted else []
            if not legacy_rows:
                return {"ok": False, "message": "没有匹配的已发布存量载荷可迁移", "missing": missing}

            batch = self.store.create_batch(
                source=source,
                scope_note=scope_note or f"来源 {source} 存量字面量迁移，共 {len(legacy_rows)} 条",
            )
            items: list[dict[str, Any]] = []
            for legacy in legacy_rows:
                raw_text = legacy["raw_text"]
                verdict = classify_legacy(source, raw_text)
                item: dict[str, Any] = {
                    "source": source,
                    "message_id": legacy["message_id"],
                    "legacy_version_id": legacy["version_id"],
                    "legacy_enc": legacy["enc"],
                    "classification_key": verdict["classification_key"],
                    "origin_format": verdict["origin_format"],
                    "convertible": verdict["convertible"],
                    "reason": verdict["reason"],
                    "raw_text": raw_text,
                    "new_version_id": None,
                    "new_enc": None,
                    "data_sha256": None,
                }
                if verdict["convertible"]:
                    data = parse_legacy(raw_text)
                    envelope = build_envelope(
                        source=source,
                        message_id=legacy["message_id"],
                        data=data,
                        origin_format=verdict["origin_format"],
                        migrated_from=raw_text,
                    )
                    new_row = self.store.append_version(
                        source=source,
                        message_id=legacy["message_id"],
                        enc=ENCODING_JSON_V2,
                        raw_text=json.dumps(
                            envelope, ensure_ascii=False, sort_keys=True
                        ),
                        envelope=envelope,
                        published=False,
                        batch_id=batch["batch_id"],
                        origin_format=verdict["origin_format"],
                        classification_key=verdict["classification_key"],
                        note="迁移预备版本（未发布）",
                    )
                    item["new_version_id"] = new_row["version_id"]
                    item["new_enc"] = ENCODING_JSON_V2
                    item["data_sha256"] = envelope["data_sha256"]
                else:
                    # 来源不明/格式不符：不生成新版本，原文留在已发布的 v1 版本上。
                    item["new_enc"] = ENCODING_UNKNOWN
                items.append(self.store.add_batch_item(batch_id=batch["batch_id"], item=item))

            return {
                "ok": True,
                "batch_id": batch["batch_id"],
                "source": source,
                "status": batch["status"],
                "total": len(items),
                "convertible": sum(1 for item in items if item["convertible"]),
                "unconvertible": sum(1 for item in items if not item["convertible"]),
                "missing": missing,
                "items": items,
            }

    def verify_before_publish(self, batch_id: int) -> dict[str, Any]:
        """切换前核对：重验每条新版本的编码版本与数据哈希，并回查原文。"""
        batch = self.store.get_batch(batch_id)
        if batch is None:
            return {"ok": False, "message": f"批次 #{batch_id} 不存在"}
        items = self.store.list_batch_items(batch_id)
        failures: list[dict[str, str]] = []
        checked = 0
        for item in items:
            if not item["convertible"]:
                # 无法转换的明细必须保留原文与非空原因说明，不能静默丢弃。
                if not item["raw_text"] or not item["reason"]:
                    failures.append({
                        "message_id": item["message_id"],
                        "problem": "不可转换明细缺少原文或原因说明",
                    })
                continue
            new_row = self.store.get_version(item["new_version_id"])
            if new_row is None or new_row["published"]:
                failures.append({
                    "message_id": item["message_id"],
                    "problem": "新版本缺失或已被意外发布",
                })
                continue
            try:
                summary = verify_envelope(new_row["envelope"])
            except EnvelopeError as exc:
                failures.append({"message_id": item["message_id"], "problem": str(exc)})
                continue
            # 回查原文：信封留存的 migrated_from 必须与旧版本逐字一致。
            legacy_row = self.store.get_version(item["legacy_version_id"])
            if legacy_row is None or new_row["envelope"].get("migrated_from") != legacy_row["raw_text"]:
                failures.append({
                    "message_id": item["message_id"],
                    "problem": "信封留存原文与存量载荷不一致",
                })
                continue
            # 回查数据：重新解析旧字面量，哈希必须与信封一致。
            try:
                re_parsed = parse_legacy(legacy_row["raw_text"])
                re_built = build_envelope(
                    source=item["source"],
                    message_id=item["message_id"],
                    data=re_parsed,
                    origin_format=item["origin_format"],
                    migrated_from=legacy_row["raw_text"],
                )
            except LegacyParseError as exc:
                failures.append({"message_id": item["message_id"], "problem": str(exc)})
                continue
            if re_built["data_sha256"] != summary["data_sha256"]:
                failures.append({
                    "message_id": item["message_id"],
                    "problem": "重算哈希与迁移信封不一致",
                })
                continue
            checked += 1
        return {
            "ok": not failures,
            "batch_id": batch_id,
            "status": batch["status"],
            "checked_versions": checked,
            "unconvertible_kept": sum(1 for item in items if not item["convertible"]),
            "failures": failures,
            "message": "核对通过，可以发布" if not failures else f"核对发现 {len(failures)} 个问题，禁止发布",
        }

    def publish_batch(self, batch_id: int) -> dict[str, Any]:
        """发布批次：先核对，通过后翻转新版本发布位。失败可撤回。"""
        report = self.verify_before_publish(batch_id)
        if not report["ok"]:
            return report
        items = self.store.list_batch_items(batch_id)
        version_ids = [
            item["new_version_id"] for item in items if item["new_version_id"] is not None
        ]
        self.store.set_published(version_ids, True)
        batch = self.store.update_batch_status(batch_id, "已发布")
        return {
            "ok": True,
            "batch_id": batch_id,
            "status": batch["status"],
            "published_versions": len(version_ids),
            "message": f"批次 #{batch_id} 已发布，回放将读取新版 JSON",
        }

    def withdraw_batch(self, batch_id: int, reason: str) -> dict[str, Any]:
        """撤回当前批次发布：新版本重新置为未发布，回放回落到仍可读的原载荷。"""
        batch = self.store.get_batch(batch_id)
        if batch is None:
            return {"ok": False, "message": f"批次 #{batch_id} 不存在"}
        if batch["status"] != "已发布":
            return {"ok": False, "message": f"批次 #{batch_id} 状态为 {batch['status']}，无需撤回"}
        items = self.store.list_batch_items(batch_id)
        version_ids = [
            item["new_version_id"] for item in items if item["new_version_id"] is not None
        ]
        self.store.set_published(version_ids, False)
        updated = self.store.update_batch_status(batch_id, "已撤回")
        return {
            "ok": True,
            "batch_id": batch_id,
            "status": updated["status"],
            "reason": reason,
            "withdrawn_versions": len(version_ids),
            "message": "批次已撤回，回放重新读取存量原载荷，新版本保留审计但不再可见",
        }

    def migration_detail(self, batch_id: int) -> dict[str, Any]:
        """迁移明细：每条消息的来源依据、分类键、新旧版本与原文留存情况。"""
        batch = self.store.get_batch(batch_id)
        if batch is None:
            return {"ok": False, "message": f"批次 #{batch_id} 不存在"}
        return {
            "ok": True,
            "batch": batch,
            "items": self.store.list_batch_items(batch_id),
        }

    # ------------------------------------------------------------------
    # 切换前核对：存量数据与示例数据双查
    # ------------------------------------------------------------------

    def pre_cutover_check(self, *, source: str, sample_message_ids: list[str]) -> dict[str, Any]:
        """上线切换前核对存量数据与示例数据。

        * 存量：该来源所有已发布载荷都能通过来源+格式核验或被明确标记保留；
        * 示例：给定示例消息已具备已发布 v2 版本且信封核验通过；
        * 对账：示例消息的 v2 数据与 v1 原文重算结果一致。
        """
        legacy_rows = self.store.list_legacy_versions(source)
        legacy_report = []
        for row in legacy_rows:
            verdict = classify_legacy(source, row["raw_text"])
            legacy_report.append({
                "message_id": row["message_id"],
                "version_id": row["version_id"],
                "convertible": verdict["convertible"],
                "classification_key": verdict["classification_key"],
                "reason": verdict["reason"],
            })

        sample_report = []
        failures: list[str] = []
        for message_id in sample_message_ids:
            versions = [
                row
                for row in self.store.list_versions(message_id)
                if row["source"] == source
            ]
            published_v2 = [
                row for row in versions
                if row["enc"] == ENCODING_JSON_V2 and row["published"]
            ]
            if not published_v2:
                failures.append(f"示例 {message_id} 缺少已发布 v2 版本")
                sample_report.append({"message_id": message_id, "ok": False})
                continue
            v2 = max(published_v2, key=lambda row: row["version_no"])
            try:
                summary = verify_envelope(v2["envelope"])
            except EnvelopeError as exc:
                failures.append(f"示例 {message_id} 信封核验失败：{exc}")
                sample_report.append({"message_id": message_id, "ok": False})
                continue
            entry = {
                "message_id": message_id,
                "ok": True,
                "version_id": v2["version_id"],
                "enc": summary["enc"],
                "data_sha256": summary["data_sha256"],
                "origin_format": summary["origin_format"],
            }
            legacy = next(
                (row for row in versions if row["enc"] == ENCODING_PY_LITERAL_V1), None
            )
            if legacy is not None:
                re_built = build_envelope(
                    source=source,
                    message_id=message_id,
                    data=parse_legacy(legacy["raw_text"]),
                    origin_format=v2["envelope"].get("origin_format"),
                    migrated_from=legacy["raw_text"],
                )
                entry["matches_legacy"] = re_built["data_sha256"] == summary["data_sha256"]
                if not entry["matches_legacy"]:
                    failures.append(f"示例 {message_id} 与存量原文对账不一致")
            sample_report.append(entry)

        return {
            "ok": not failures,
            "source": source,
            "origin": describe_origin(source),
            "legacy_total": len(legacy_report),
            "legacy_convertible": sum(1 for row in legacy_report if row["convertible"]),
            "legacy_kept": sum(1 for row in legacy_report if not row["convertible"]),
            "legacy_report": legacy_report,
            "sample_report": sample_report,
            "failures": failures,
        }

    # ------------------------------------------------------------------
    # 入口二：回放（受理冻结水位、续跑、重跑、回滚对账）
    # ------------------------------------------------------------------

    def open_replay(self, *, source: str, target_topic: str) -> dict[str, Any]:
        """受理回放请求：冻结当前水位作为可见性上界。"""
        allowed, message = ingest_origin(source)
        if not allowed:
            return {"ok": False, "message": message}
        request = self.store.create_replay_request(
            source=source,
            target_topic=target_topic,
            as_of_seq=self.store.current_seq(),
        )
        return {
            "ok": True,
            "request_id": request["request_id"],
            "source": source,
            "target_topic": target_topic,
            "cutoff_seq": request["cutoff_seq"],
            "message": f"回放请求已受理，冻结水位 seq={request['cutoff_seq']}，后续新写入不可见",
        }

    def _materialize(self, row: dict[str, Any]) -> dict[str, Any]:
        """把一个可见版本统一物化为可交付内容，并标注实际读取的编码版本。"""
        if row["enc"] == ENCODING_JSON_V2:
            summary = verify_envelope(row["envelope"])
            return {
                "message_id": row["message_id"],
                "source": row["source"],
                "read_enc": ENCODING_JSON_V2,
                "version_id": row["version_id"],
                "seq": row["seq"],
                "data": row["envelope"]["data"],
                "data_sha256": summary["data_sha256"],
                "origin_format": summary.get("origin_format"),
            }
        if row["enc"] == ENCODING_PY_LITERAL_V1:
            # 仍可读的原载荷：安全解析后交付，读取编码如实记录为 v1。
            verdict = classify_legacy(row["source"], row["raw_text"])
            data = parse_legacy(row["raw_text"])
            return {
                "message_id": row["message_id"],
                "source": row["source"],
                "read_enc": ENCODING_PY_LITERAL_V1,
                "version_id": row["version_id"],
                "seq": row["seq"],
                "data": data,
                "data_sha256": None,
                "origin_format": verdict["origin_format"],
                "note": "读取自存量 Python 字面量原载荷",
            }
        # 不可转换载荷：保留原文交付，不伪装成已转换数据。
        return {
            "message_id": row["message_id"],
            "source": row["source"],
            "read_enc": ENCODING_UNKNOWN,
            "version_id": row["version_id"],
            "seq": row["seq"],
            "data": None,
            "raw_text": row["raw_text"],
            "data_sha256": None,
            "note": "来源不明无法转换，保留原文",
        }

    def run_replay(self, request_id: int, *, limit: int | None = None) -> dict[str, Any]:
        """执行（或中断后续跑）一次回放；已交付消息自动跳过。"""
        request = self.store.get_replay_request(request_id)
        if request is None:
            return {"ok": False, "message": f"回放请求 #{request_id} 不存在"}
        # 已完成请求允许重跑：可见消息都已在台账中，下面的循环会全部跳过，
        # 返回 delivered_this_run=0，天然幂等，无需单独分支。

        visible = self.store.replay_view(
            source=request["source"], as_of_seq=request["cutoff_seq"]
        )
        already = self.store.delivered_message_ids(request_id)
        self.store.update_replay_request(request_id, status="回放中")
        delivered: list[dict[str, Any]] = []
        for row in visible:
            if row["message_id"] in already:
                continue
            material = self._materialize(row)
            ledger = self.store.record_delivery(
                request_id=request_id,
                source=request["source"],
                message_id=material["message_id"],
                version_id=material["version_id"],
                enc=material["read_enc"],
                data_sha256=material.get("data_sha256"),
                origin_seq=row["seq"],
            )
            delivered.append({**material, "deduped": ledger.get("deduped", False)})
            if limit is not None and len(delivered) >= limit:
                return {
                    "ok": True,
                    "request_id": request_id,
                    "status": "回放中断",
                    "delivered_this_run": len(delivered),
                    "delivered_total": request["delivered_count"] + len(delivered),
                    "cutoff_seq": request["cutoff_seq"],
                    "delivered": delivered,
                    "message": "达到本次处理上限，进度已落台账，可随时续跑",
                }

        refreshed = self.store.get_replay_request(request_id)
        self.store.update_replay_request(request_id, status="已完成")
        return {
            "ok": True,
            "request_id": request_id,
            "status": "已完成",
            "delivered_this_run": len(delivered),
            "delivered_total": refreshed["delivered_count"],
            "cutoff_seq": request["cutoff_seq"],
            "delivered": delivered,
            "message": "回放完成，交付范围未超过受理水位",
        }

    def interrupt_replay(self, request_id: int, reason: str) -> dict[str, Any]:
        """显式中断回放；已交付部分保留在台账，恢复时从断点继续。"""
        request = self.store.get_replay_request(request_id)
        if request is None:
            return {"ok": False, "message": f"回放请求 #{request_id} 不存在"}
        self.store.update_replay_request(request_id, status="回放中断", interrupt_reason=reason)
        return {
            "ok": True,
            "request_id": request_id,
            "status": "回放中断",
            "reason": reason,
            "delivered_total": request["delivered_count"],
            "message": "中断已记录，台账保留消息身份与版本，恢复不会重复交付",
        }

    def resume_replay(self, request_id: int) -> dict[str, Any]:
        """中断恢复：按同一受理水位续跑，已交付消息经台账去重。"""
        request = self.store.get_replay_request(request_id)
        if request is None:
            return {"ok": False, "message": f"回放请求 #{request_id} 不存在"}
        return self.run_replay(request_id)

    def reconcile(self, request_id: int) -> dict[str, Any]:
        """回滚/重跑后对账：核对实际交付与当前可见版本、确认范围不越界。"""
        request = self.store.get_replay_request(request_id)
        if request is None:
            return {"ok": False, "message": f"回放请求 #{request_id} 不存在"}
        ledger = self.store.list_ledger(request_id)
        visible = {
            row["message_id"]: row
            for row in self.store.replay_view(
                source=request["source"], as_of_seq=request["cutoff_seq"]
            )
        }
        problems: list[str] = []
        entries = []
        for record in ledger:
            current = visible.get(record["message_id"])
            if current is None:
                problems.append(
                    f"消息 {record['message_id']} 已交付但当前水位下无可见版本（可能批次已撤回）"
                )
                continue
            entry = {
                "message_id": record["message_id"],
                "delivered_version_id": record["version_id"],
                "delivered_enc": record["enc"],
                "current_version_id": current["version_id"],
                "current_enc": current["enc"],
                "origin_seq": record["origin_seq"],
                "same_version": record["version_id"] == current["version_id"],
            }
            if record["origin_seq"] > request["cutoff_seq"]:
                problems.append(
                    f"消息 {record['message_id']} 的交付序号越过受理水位，确认范围被扩大"
                )
            if not entry["same_version"]:
                # 批次撤回/重发布导致可见版本漂移：实际交付仍可按台账核对，
                # 但必须显式标记，避免把回退后的版本误当成已交付版本。
                problems.append(
                    f"消息 {record['message_id']} 交付版本为 {record['enc']}"
                    f"（#{record['version_id']}），当前可见版本已变为 "
                    f"{current['enc']}（#{current['version_id']}），存在回滚/重发布漂移"
                )
            entries.append(entry)
        missing = sorted(set(visible) - {record["message_id"] for record in ledger})
        return {
            "ok": not problems,
            "request_id": request_id,
            "status": request["status"],
            "cutoff_seq": request["cutoff_seq"],
            "confirmed_seq": request["confirmed_seq"],
            "delivered_count": len(ledger),
            "visible_count": len(visible),
            "undelivered_message_ids": missing,
            "entries": entries,
            "problems": problems,
        }

    # ------------------------------------------------------------------
    # 查询视图
    # ------------------------------------------------------------------

    def stored_versions(self, source: str | None = None) -> list[dict[str, Any]]:
        """查询某来源当前各消息可见的版本与编码（演示「能查询到的版本和数据」）。"""
        cutoff = self.store.current_seq()
        if source is None:
            sources = sorted({row["source"] for row in self.store.payload_versions})
            out = []
            for name in sources:
                out.extend(self.store.replay_view(source=name, as_of_seq=cutoff))
            return out
        return self.store.replay_view(source=source, as_of_seq=cutoff)


_SERVICE: MigrationService | None = None


def get_migration_service() -> MigrationService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = MigrationService(get_migration_store())
    return _SERVICE
