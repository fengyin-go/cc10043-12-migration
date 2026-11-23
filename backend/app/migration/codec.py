"""载荷编解码：旧 Python 字面量解析、来源核验、版本化 JSON 信封。

信封（envelope）是新接入与迁移产物的统一载体::

    {
      "enc": "json.envelope.v2",          # 可核验的编码版本
      "source": "src.order_connector_a",  # 接入来源
      "message_id": "...",                # 原消息身份
      "origin_format": "python_literal_repr_v1",  # 原写入格式（迁移产物才有）
      "migrated_from": "{'ok': True ...}",        # 原文留存（迁移产物才有）
      "data": {...},                      # 规范化后的 JSON 数据
      "data_sha256": "..."                # data 的内容哈希，防篡改/可重验
    }

设计要点
========

* ``ast.literal_eval`` 只接受字面量，不执行任何调用表达式，从根上避免把
  历史载荷当代码执行。
* 旧格式核验同时依赖**来源登记的格式**与**文本书写特征**，文本相同但来源
  不同的载荷走不同的核验依据，分类键是 ``(source, origin_format)``。
* 信封自带编码版本和数据哈希；读取时先验版本再验哈希，任一不符即拒绝。
* 来源不明或格式核验不通过时，不做转换，由上层保留原文并记录无法转换。
"""
from __future__ import annotations

import ast
import hashlib
import json
from typing import Any

from app.migration.registry import (
    LITERAL_FORMATS,
    PY_LITERAL_REPR_V1,
    SourceSpec,
    source_spec,
)

# ---------------------------------------------------------------------------
# 编码版本常量
# ---------------------------------------------------------------------------

#: 历史连接器直写的 Python 字面量（无信封、无版本）。
ENCODING_PY_LITERAL_V1 = "python_literal_repr_v1"

#: 新版带版本标识的 JSON 信封。
ENCODING_JSON_V2 = "json.envelope.v2"

#: 无法核验的编码（来源不明 / 格式不符）。
ENCODING_UNKNOWN = "unknown.unchanged.v0"


class EnvelopeError(ValueError):
    """信封缺失、版本不符或哈希校验失败时抛出。"""


class LegacyParseError(ValueError):
    """旧字面量无法按登记格式解析时抛出。"""


# ---------------------------------------------------------------------------
# 旧 Python 字面量解析
# ---------------------------------------------------------------------------


def parse_legacy(raw_text: str) -> Any:
    """把连接器写入的 Python 字面量安全解析为 Python 对象。

    只允许字面量（dict/list/tuple/str/int/float/bool/None），历史订单里的
    ``True`` / ``None`` 在这里被正确还原为 ``True`` / ``None``，再由 JSON
    序列化转成 ``true`` / ``null``。
    """
    try:
        return ast.literal_eval(raw_text)
    except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
        raise LegacyParseError(f"Python 字面量解析失败：{exc}") from exc


def classify_legacy(source: str, raw_text: str) -> dict[str, Any]:
    """按接入来源 + 原写入格式核验一段存量载荷。

    返回核验结论字典，包含：

    ``convertible``
        是否可转换；
    ``encoding``
        核验出的编码版本（无法核验时为 :data:`ENCODING_UNKNOWN`）；
    ``reason``
        结论说明，来源不明时明确写出「保留原文、无法转换」；
    ``origin_format``
        登记的原写入格式 ID；
    ``classification_key``
        分类键 ``(source, origin_format)``——文本相同但来源不同的载荷
        分类键不同，不会被归并。
    """
    spec: SourceSpec | None = source_spec(source)
    if spec is None:
        return {
            "source": source,
            "convertible": False,
            "encoding": ENCODING_UNKNOWN,
            "origin_format": None,
            "classification_key": f"{source}::UNREGISTERED",
            "reason": "来源未登记，保留原文并标记无法转换，不做格式猜测",
        }

    origin_format = spec.origin_format
    if not origin_format.matches(raw_text):
        return {
            "source": source,
            "convertible": False,
            "encoding": ENCODING_UNKNOWN,
            "origin_format": origin_format.format_id,
            "classification_key": f"{source}::{origin_format.format_id}::marker-mismatch",
            "reason": (
                f"来源登记格式为 {origin_format.format_id}，但文本缺少该格式特征标记"
                "（True/None/单引号容器等），保留原文，无法转换"
            ),
        }

    try:
        parse_legacy(raw_text)
    except LegacyParseError as exc:
        return {
            "source": source,
            "convertible": False,
            "encoding": ENCODING_UNKNOWN,
            "origin_format": origin_format.format_id,
            "classification_key": f"{source}::{origin_format.format_id}::unparseable",
            "reason": f"格式特征相符但字面量解析失败，保留原文：{exc}",
        }

    return {
        "source": source,
        "convertible": True,
        "encoding": ENCODING_PY_LITERAL_V1,
        "origin_format": origin_format.format_id,
        "classification_key": f"{source}::{origin_format.format_id}",
        "reason": f"按来源 {source} 登记的 {origin_format.format_id} 核验通过",
    }


# ---------------------------------------------------------------------------
# 版本化 JSON 信封
# ---------------------------------------------------------------------------


def _canonical_digest(data: Any) -> str:
    """对 data 做规范 JSON 序列化后求 SHA-256。

    排序键、统一分隔符，保证同一数据无论从哪条路径生成，哈希都一致，
    迁移产物与新接入产物可重验、可对账。
    """
    canonical = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_envelope(
    *,
    source: str,
    message_id: str,
    data: Any,
    origin_format: str | None = None,
    migrated_from: str | None = None,
) -> dict[str, Any]:
    """构造带版本标识与数据哈希的 JSON 信封。"""
    envelope = {
        "enc": ENCODING_JSON_V2,
        "source": source,
        "message_id": message_id,
        "data": data,
        "data_sha256": _canonical_digest(data),
    }
    if origin_format is not None:
        envelope["origin_format"] = origin_format
    if migrated_from is not None:
        # 原文随信封留存，旧载荷在迁移后仍可按原写入格式复核。
        envelope["migrated_from"] = migrated_from
    return envelope


def verify_envelope(envelope: Any, *, expected_encoding: str = ENCODING_JSON_V2) -> dict[str, Any]:
    """核验信封的编码版本与数据哈希，返回信封摘要；失败抛 :class:`EnvelopeError`。"""
    if not isinstance(envelope, dict):
        raise EnvelopeError("载荷不是 JSON 对象")
    enc = envelope.get("enc")
    if enc != expected_encoding:
        raise EnvelopeError(
            f"编码版本不可核验：期望 {expected_encoding}，实际 {enc!r}"
        )
    for field in ("source", "message_id", "data", "data_sha256"):
        if field not in envelope:
            raise EnvelopeError(f"信封缺少必填字段：{field}")
    actual_digest = _canonical_digest(envelope["data"])
    if not _hash_equals(actual_digest, str(envelope["data_sha256"])):
        raise EnvelopeError(
            "data_sha256 校验失败：数据与哈希不一致，载荷可能已被篡改"
        )
    return {
        "enc": enc,
        "source": envelope["source"],
        "message_id": envelope["message_id"],
        "origin_format": envelope.get("origin_format"),
        "has_legacy_text": "migrated_from" in envelope,
        "data_sha256": actual_digest,
    }


def _hash_equals(actual: str, expected: str) -> bool:
    """恒定时长比较，避免哈希校验被计时侧信道利用。"""
    if len(actual) != len(expected):
        return False
    result = 0
    for left, right in zip(actual, expected):
        result |= ord(left) ^ ord(right)
    return result == 0


def parse_payload(raw_text: str) -> tuple[str, Any]:
    """回放入口的统一读取：识别一段载荷当前是哪种编码。

    返回 ``(encoding, parsed)``：

    * 新版 JSON 信封：验版本 + 验哈希后返回 ``(json.envelope.v2, envelope)``；
    * 旧 Python 字面量：返回 ``(python_literal_repr_v1, 解析后的对象)``，
      调用方仍可拿到原文做来源核验；
    * 两者都不成立：返回 ``(unknown.unchanged.v0, 原文本)``，保留原文。
    """
    stripped = raw_text.strip()
    # 先尝试 JSON：新版信封必须能通过严格版本与哈希核验才算数。
    if stripped.startswith("{"):
        try:
            candidate = json.loads(stripped)
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict) and candidate.get("enc") == ENCODING_JSON_V2:
            verify_envelope(candidate)
            return ENCODING_JSON_V2, candidate

    # 再尝试旧字面量：只做安全解析，不做来源归属（归属由调用方结合来源核验）。
    if PY_LITERAL_REPR_V1.matches(stripped):
        try:
            return ENCODING_PY_LITERAL_V1, parse_legacy(stripped)
        except LegacyParseError:
            pass

    return ENCODING_UNKNOWN, raw_text


def known_origin_formats() -> dict[str, str]:
    """导出已登记的原写入格式，供接口与文档核对。"""
    return {fmt.format_id: fmt.display_name for fmt in LITERAL_FORMATS.values()}
