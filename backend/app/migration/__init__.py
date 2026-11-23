"""载荷编码迁移与版本化回放入口子系统。

背景
----
存量采集载荷由连接器以 Python 字面量（``True`` / ``None`` / ``{'k': v}``）
写入，JSON 读取方无法直接解析。本子系统把存量载荷迁移为带版本标识的 JSON
信封（envelope），同时保证：

* 新旧采集批次都能被回放入口读取（可核验的编码版本或仍可读的原载荷）；
* 旧载荷按「接入来源 + 原写入格式」核验，来源不明时保留原文并说明无法转换，
  不会把文本相同但来源依据不同的载荷归为同一类；
* 迁移只追加新版本、不改写原载荷，批次可发布也可撤回；
* 回放请求绑定消息身份与受理水位，分段迁移期间的读写冲突由追加式存储和
  单调序号隔离，旧的已确认范围不会被新版输入扩大。

模块划分见各文件 docstring。
"""

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
from app.migration.registry import (
    LITERAL_FORMATS,
    OriginFormat,
    SourceSpec,
    ingest_origin,
    source_spec,
)
from app.migration.store import MigrationStore, get_migration_store, reset_migration_store
from app.migration.service import MigrationService, get_migration_service

__all__ = [
    "ENCODING_JSON_V2",
    "ENCODING_PY_LITERAL_V1",
    "ENCODING_UNKNOWN",
    "EnvelopeError",
    "LITERAL_FORMATS",
    "LegacyParseError",
    "OriginFormat",
    "SourceSpec",
    "build_envelope",
    "classify_legacy",
    "get_migration_service",
    "get_migration_store",
    "ingest_origin",
    "parse_legacy",
    "parse_payload",
    "reset_migration_store",
    "source_spec",
    "verify_envelope",
    "MigrationService",
    "MigrationStore",
]
