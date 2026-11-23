"""接入来源登记表：来源与原写入格式的核验依据。

关键约束（来自迁移需求）
========================

1. 旧载荷必须**按接入来源和原写入格式**核验，而不是只看文本长相。
   两个来源可以写出内容完全相同的字面量，但它们的迁移依据不同，必须生成
   两条独立明细，不能合并成同一类。
2. 来源不明的载荷保留原文，登记为无法转换，绝不猜测归属。
3. 每种来源登记其历史写入格式（本平台历史上只有连接器直写的 Python 字面量
   一种格式，但仍以显式格式 ID 记录，避免「看起来像就归并」）。

来源登记表是迁移的事实依据：迁移批次只接受登记在册的来源，回放入口也用
同一张表判定一段原始载荷该按哪种编码解读。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 原写入格式登记
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OriginFormat:
    """一种历史写入格式的核验依据。"""

    format_id: str
    """格式标识，写入迁移明细，作为核验锚点。"""

    display_name: str
    markers: tuple[str, ...]
    """该格式的特征标记（如 Python 字面量里的 True/None）。"""

    def matches(self, raw_text: str) -> bool:
        """核验文本是否符合该格式的书写特征。"""
        return any(marker in raw_text for marker in self.markers)


#: 连接器直写的 Python 字面量：历史订单载荷里保留 True / None 的那种格式。
PY_LITERAL_REPR_V1 = OriginFormat(
    format_id="python_literal_repr_v1",
    display_name="连接器Python字面量repr(v1)",
    # True / None / 单引号字典都是 Python repr 与 JSON 的明确分界点。
    markers=("True", "False", "None", "{'", "['"),
)

#: 来源登记表支持的历史格式索引。
LITERAL_FORMATS: dict[str, OriginFormat] = {
    PY_LITERAL_REPR_V1.format_id: PY_LITERAL_REPR_V1,
}


# ---------------------------------------------------------------------------
# 接入来源登记
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    """一个接入来源的迁移依据。"""

    source: str
    """来源编码，随消息身份一起落库。"""

    source_name: str
    origin_format: OriginFormat
    """该来源历史载荷的原写入格式。"""

    note: str = ""


#: 已登记来源。即使两个来源的载荷文本一模一样，这里的来源编码和格式依据
#: 不同，迁移明细与消息身份就必须分开。
SOURCE_REGISTRY: dict[str, SourceSpec] = {
    "src.order_connector_a": SourceSpec(
        source="src.order_connector_a",
        source_name="订单连接器A（历史Python字面量写入）",
        origin_format=PY_LITERAL_REPR_V1,
        note="A 连接器早期版本直接写 repr(dict)，布尔与空值为 True/None。",
    ),
    "src.order_connector_b": SourceSpec(
        source="src.order_connector_b",
        source_name="订单连接器B（历史Python字面量写入）",
        origin_format=PY_LITERAL_REPR_V1,
        note="B 连接器复用同一序列化习惯，但属于独立接入来源，单独核验。",
    ),
}


def source_spec(source: str) -> SourceSpec | None:
    """返回来源登记；未登记来源返回 None（调用方按来源不明处理）。"""
    return SOURCE_REGISTRY.get(source)


def ingest_origin(source: str) -> tuple[bool, str]:
    """接入入口核验来源是否允许写入。

    返回 (是否允许, 说明)。未登记来源拒绝写入，从入口处防止来源不明数据
    混入版本化存储。
    """
    spec = SOURCE_REGISTRY.get(source)
    if spec is None:
        return False, f"来源 {source} 未登记，拒绝接入；请先在来源登记表登记写入格式"
    return True, f"来源 {spec.source_name} 核验通过，历史格式：{spec.origin_format.format_id}"


def describe_origin(source: str) -> dict[str, Any]:
    """给迁移明细/接口用的来源视图。"""
    spec = SOURCE_REGISTRY.get(source)
    if spec is None:
        return {
            "source": source,
            "registered": False,
            "source_name": "未知来源",
            "origin_format": None,
            "note": "来源未登记，保留原文，无法转换",
        }
    return {
        "source": spec.source,
        "registered": True,
        "source_name": spec.source_name,
        "origin_format": spec.origin_format.format_id,
        "origin_format_name": spec.origin_format.display_name,
        "note": spec.note,
    }
