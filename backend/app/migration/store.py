"""追加式版本化载荷仓。

存储模型（全部内存表，进程内线程安全；真实部署可整体替换为数据库实现）
=====================================================================

``payload_versions``
    一条载荷的**一个版本**一行，迁移只追加新版本、永不改写或删除旧版本：

    * 存量载荷装载进来是一条 ``python_literal_repr_v1`` 版本，``published=1``；
    * 迁移预备生成一条 ``json.envelope.v2`` 版本，``published=0``，批次发布
      之后才置 1；撤回重新置 0；
    * 新接入直接写 ``json.envelope.v2`` 且 ``published=1``。

    每条版本带单调递增的 ``seq``（写入顺序）和每条消息独立的 ``version_no``。

``migration_batches`` / ``migration_items``
    迁移批次与明细。明细关联到原消息身份、旧版本 ID、新版本 ID 与分类键，
    撤回只翻发布位，审计链完整保留。

``replay_requests`` / ``delivery_ledger``
    回放请求受理时冻结水位 ``cutoff_seq``；交付台账按 (请求, 消息身份) 记录
    实际交付的版本与哈希，重跑去重、中断续跑、回滚后对账都依赖它。

冲突隔离
========

* 所有读写经同一把可重入锁，读操作以快照形式取出后在锁外回放；
* 回放可见性 = ``published=1 且 seq <= cutoff_seq`` 后取消息最新版本。
  分段迁移时未发布的新版本、以及受理之后才写入的新消息，对旧请求都不可见，
  旧的已确认范围不会被新版输入扩大。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from app.migration.codec import ENCODING_JSON_V2, ENCODING_PY_LITERAL_V1, verify_envelope


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MigrationStore:
    """线程安全的追加式版本化载荷仓。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seq = 0
        self._version_id = 0
        self._batch_id = 0
        self._item_id = 0
        self._request_id = 0
        self._ledger_id = 0
        self.payload_versions: list[dict[str, Any]] = []
        self.migration_batches: list[dict[str, Any]] = []
        self.migration_items: list[dict[str, Any]] = []
        self.replay_requests: list[dict[str, Any]] = []
        self.delivery_ledger: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _snapshot(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def current_seq(self) -> int:
        with self._lock:
            return self._seq

    # ------------------------------------------------------------------
    # 载荷版本：追加，不改写
    # ------------------------------------------------------------------

    def append_version(
        self,
        *,
        source: str,
        message_id: str,
        enc: str,
        raw_text: str,
        envelope: dict[str, Any] | None,
        published: bool,
        batch_id: int | None = None,
        note: str = "",
        origin_format: str | None = None,
        classification_key: str | None = None,
    ) -> dict[str, Any]:
        """追加一条载荷版本。"""
        with self._lock:
            self._version_id += 1
            version_no = (
                sum(
                    1
                    for row in self.payload_versions
                    if row["message_id"] == message_id and row["source"] == source
                )
                + 1
            )
            row = {
                "version_id": self._version_id,
                "version_no": version_no,
                "seq": self._next_seq(),
                "source": source,
                "message_id": message_id,
                "enc": enc,
                "raw_text": raw_text,
                "envelope": envelope,
                "published": bool(published),
                "batch_id": batch_id,
                "origin_format": origin_format,
                "classification_key": classification_key,
                "note": note,
                "created_at": _now(),
            }
            self.payload_versions.append(row)
            return dict(row)

    def load_legacy_payload(self, *, source: str, message_id: str, raw_text: str) -> dict[str, Any]:
        """装载一条存量 Python 字面量载荷（天然可读，默认已发布）。"""
        return self.append_version(
            source=source,
            message_id=message_id,
            enc=ENCODING_PY_LITERAL_V1,
            raw_text=raw_text,
            envelope=None,
            published=True,
            note="存量连接器字面量载荷",
        )

    def append_ingest_envelope(self, *, source: str, envelope: dict[str, Any]) -> dict[str, Any]:
        """新接收入口写入一条已发布的版本化 JSON 载荷。"""
        summary = verify_envelope(envelope)
        if envelope["source"] != source:
            raise ValueError("信封 source 与接入来源不一致，拒绝写入")
        return self.append_version(
            source=source,
            message_id=str(envelope["message_id"]),
            enc=ENCODING_JSON_V2,
            raw_text=json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            envelope=dict(envelope),
            published=True,
            origin_format=summary.get("origin_format"),
            note="新接入版本化JSON",
        )

    def set_published(self, version_ids: list[int], published: bool) -> None:
        """批量翻转发布位（发布/撤回共用，不删除任何版本）。"""
        target = set(version_ids)
        with self._lock:
            for row in self.payload_versions:
                if row["version_id"] in target:
                    row["published"] = published

    def get_version(self, version_id: int) -> dict[str, Any] | None:
        with self._lock:
            for row in self.payload_versions:
                if row["version_id"] == version_id:
                    return dict(row)
        return None

    def list_versions(self, message_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._snapshot(
                [
                    row
                    for row in self.payload_versions
                    if row["message_id"] == message_id
                ]
            )

    def list_legacy_versions(self, source: str | None = None) -> list[dict[str, Any]]:
        """取出仍以旧编码承载的已发布载荷，供分段迁移挑选。"""
        with self._lock:
            rows = [
                row
                for row in self.payload_versions
                if row["enc"] == ENCODING_PY_LITERAL_V1
                and row["published"]
                and (source is None or row["source"] == source)
            ]
            return self._snapshot(rows)

    # ------------------------------------------------------------------
    # 回放可见性：发布位 + 受理水位 + 每消息取最新
    # ------------------------------------------------------------------

    def replay_view(self, *, source: str, as_of_seq: int) -> list[dict[str, Any]]:
        """返回某来源在指定水位下回放应读取的每消息最新可见版本。

        不可见的情况：版本未发布（迁移未完成/已撤回）、``seq > as_of_seq``
        （受理之后才写入）。消息身份相同的多个可见版本取 ``version_no`` 最大者。
        """
        with self._lock:
            chosen: dict[str, dict[str, Any]] = {}
            for row in self.payload_versions:
                if row["source"] != source:
                    continue
                if not row["published"] or row["seq"] > as_of_seq:
                    continue
                current = chosen.get(row["message_id"])
                if current is None or row["version_no"] > current["version_no"]:
                    chosen[row["message_id"]] = dict(row)
            return [chosen[key] for key in sorted(chosen)]

    # ------------------------------------------------------------------
    # 迁移批次与明细
    # ------------------------------------------------------------------

    def create_batch(self, *, source: str, scope_note: str) -> dict[str, Any]:
        with self._lock:
            self._batch_id += 1
            batch = {
                "batch_id": self._batch_id,
                "source": source,
                "status": "待发布",
                "scope_note": scope_note,
                "created_at": _now(),
                "published_at": None,
                "withdrawn_at": None,
                "item_count": 0,
                "convertible_count": 0,
                "unconvertible_count": 0,
            }
            self.migration_batches.append(batch)
            return dict(batch)

    def add_batch_item(self, *, batch_id: int, item: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._item_id += 1
            row = {"item_id": self._item_id, "batch_id": batch_id, **item}
            self.migration_items.append(row)
            batch = next(b for b in self.migration_batches if b["batch_id"] == batch_id)
            batch["item_count"] += 1
            if item.get("convertible"):
                batch["convertible_count"] += 1
            else:
                batch["unconvertible_count"] += 1
            return dict(row)

    def update_batch_status(self, batch_id: int, status: str) -> dict[str, Any] | None:
        with self._lock:
            for batch in self.migration_batches:
                if batch["batch_id"] == batch_id:
                    batch["status"] = status
                    if status == "已发布":
                        batch["published_at"] = _now()
                    elif status == "已撤回":
                        batch["withdrawn_at"] = _now()
                    return dict(batch)
        return None

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        with self._lock:
            for batch in self.migration_batches:
                if batch["batch_id"] == batch_id:
                    return dict(batch)
        return None

    def list_batches(self, source: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                dict(batch)
                for batch in self.migration_batches
                if source is None or batch["source"] == source
            ]
            return rows

    def list_batch_items(self, batch_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return self._snapshot(
                [row for row in self.migration_items if row["batch_id"] == batch_id]
            )

    def active_batch_id(self, source: str) -> int | None:
        """同一来源同时只允许一个待发布批次，串行化分段迁移写入。"""
        with self._lock:
            for batch in self.migration_batches:
                if batch["source"] == source and batch["status"] == "待发布":
                    return batch["batch_id"]
        return None

    # ------------------------------------------------------------------
    # 回放请求与交付台账
    # ------------------------------------------------------------------

    def create_replay_request(self, *, source: str, target_topic: str, as_of_seq: int) -> dict[str, Any]:
        with self._lock:
            self._request_id += 1
            request = {
                "request_id": self._request_id,
                "source": source,
                "target_topic": target_topic,
                "cutoff_seq": as_of_seq,
                "status": "待回放",
                "confirmed_seq": 0,
                "delivered_count": 0,
                "created_at": _now(),
            }
            self.replay_requests.append(request)
            return dict(request)

    def get_replay_request(self, request_id: int) -> dict[str, Any] | None:
        with self._lock:
            for request in self.replay_requests:
                if request["request_id"] == request_id:
                    return dict(request)
        return None

    def update_replay_request(self, request_id: int, **fields: Any) -> dict[str, Any] | None:
        with self._lock:
            for request in self.replay_requests:
                if request["request_id"] == request_id:
                    request.update(fields)
                    return dict(request)
        return None

    def list_replay_requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._snapshot(self.replay_requests)

    def delivered_message_ids(self, request_id: int) -> set[str]:
        with self._lock:
            return {
                row["message_id"]
                for row in self.delivery_ledger
                if row["request_id"] == request_id
            }

    def record_delivery(
        self,
        *,
        request_id: int,
        source: str,
        message_id: str,
        version_id: int,
        enc: str,
        data_sha256: str | None,
        origin_seq: int,
    ) -> dict[str, Any]:
        """登记一条实际交付；同一请求同一消息身份已交付则幂等跳过。"""
        with self._lock:
            for row in self.delivery_ledger:
                if row["request_id"] == request_id and row["message_id"] == message_id:
                    return dict(row) | {"deduped": True}
            self._ledger_id += 1
            entry = {
                "ledger_id": self._ledger_id,
                "request_id": request_id,
                "source": source,
                "message_id": message_id,
                "version_id": version_id,
                "enc": enc,
                "data_sha256": data_sha256,
                "origin_seq": origin_seq,
                "delivered_at": _now(),
            }
            self.delivery_ledger.append(entry)
            request = next(
                r for r in self.replay_requests if r["request_id"] == request_id
            )
            request["delivered_count"] += 1
            request["confirmed_seq"] = max(request["confirmed_seq"], origin_seq)
            return dict(entry)

    def list_ledger(self, request_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return self._snapshot(
                [
                    row
                    for row in self.delivery_ledger
                    if request_id is None or row["request_id"] == request_id
                ]
            )

    def reset(self) -> None:
        with self._lock:
            self._seq = 0
            self._version_id = self._item_id = self._batch_id = 0
            self._request_id = self._ledger_id = 0
            self.payload_versions.clear()
            self.migration_batches.clear()
            self.migration_items.clear()
            self.replay_requests.clear()
            self.delivery_ledger.clear()


_STORE: MigrationStore | None = None
_STORE_LOCK = threading.Lock()


def get_migration_store() -> MigrationStore:
    """进程级单例载荷仓。"""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = MigrationStore()
    return _STORE


def reset_migration_store() -> MigrationStore:
    """测试/演示用：清空并重建单例。"""
    global _STORE
    with _STORE_LOCK:
        _STORE = MigrationStore()
        return _STORE
