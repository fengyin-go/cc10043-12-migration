"""载荷编码迁移与版本化回放的 HTTP 入口。

路由分两组，对应上线切换的两个入口：

* ``/api/migration/...`` 接入与迁移批次（含切换前核对、发布、撤回）；
* ``/api/replay/...``     回放请求（受理、执行/续跑、中断、对账）。

路由层只做参数解包，业务判断全部在 :class:`MigrationService` 内。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.migration.codec import known_origin_formats
from app.migration.registry import describe_origin
from app.migration.service import get_migration_service

router = APIRouter(prefix="/api", tags=["载荷编码迁移与回放"])


class LegacyLoadPayload(BaseModel):
    source: str
    message_id: str
    raw_text: str = Field(..., description="连接器历史写入的 Python 字面量原文")


class IngressPayload(BaseModel):
    source: str
    raw_json_text: str = Field(..., description="带 enc 版本标识的 JSON 信封文本")


class PreparePayload(BaseModel):
    source: str
    message_ids: list[str] | None = None
    scope_note: str = ""


class WithdrawPayload(BaseModel):
    reason: str


class ReplayOpenPayload(BaseModel):
    source: str
    target_topic: str


class InterruptPayload(BaseModel):
    reason: str


class ReplayRunPayload(BaseModel):
    limit: int | None = None


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    return result


# ---------------------------------------------------------------------------
# 元数据与存量装载
# ---------------------------------------------------------------------------


@router.get("/migration/sources")
def list_sources() -> dict[str, Any]:
    """来源登记表与已登记的原写入格式。"""
    from app.migration.registry import SOURCE_REGISTRY

    return {
        "origin_formats": known_origin_formats(),
        "sources": [describe_origin(source) for source in sorted(SOURCE_REGISTRY)],
    }

@router.post("/migration/legacy/load")
def load_legacy(payload: LegacyLoadPayload) -> dict[str, Any]:
    """装载一条存量字面量载荷（来源不明也保留原文并打标）。"""
    service = get_migration_service()
    return _ok(service.load_legacy(
        source=payload.source, message_id=payload.message_id, raw_text=payload.raw_text
    ))


@router.get("/migration/versions")
def list_versions(source: str | None = None) -> dict[str, Any]:
    """查询当前各消息可见的版本与编码。"""
    service = get_migration_service()
    rows = service.stored_versions(source)
    return {"items": rows, "total": len(rows)}


# ---------------------------------------------------------------------------
# 入口一：新接入
# ---------------------------------------------------------------------------


@router.post("/ingress/json")
def ingress_json(payload: IngressPayload) -> dict[str, Any]:
    """新接收入口：只接受通过核验的版本化 JSON 信封。"""
    return _ok(get_migration_service().ingress_json(
        source=payload.source, raw_json_text=payload.raw_json_text
    ))


# ---------------------------------------------------------------------------
# 迁移批次
# ---------------------------------------------------------------------------


@router.post("/migration/prepare")
def prepare_migration(payload: PreparePayload) -> dict[str, Any]:
    """预备迁移：生成未发布 v2 版本与逐条来源/格式核验明细。"""
    return _ok(get_migration_service().prepare_migration(
        source=payload.source, message_ids=payload.message_ids, scope_note=payload.scope_note
    ))


@router.post("/migration/batches/{batch_id}/verify")
def verify_batch(batch_id: int) -> dict[str, Any]:
    """切换前核对批次：重验版本、哈希与原文一致性。"""
    return _ok(get_migration_service().verify_before_publish(batch_id))


@router.post("/migration/batches/{batch_id}/publish")
def publish_batch(batch_id: int) -> dict[str, Any]:
    """发布批次（内部先核对，失败返回问题清单，不产生发布）。"""
    return _ok(get_migration_service().publish_batch(batch_id))


@router.post("/migration/batches/{batch_id}/withdraw")
def withdraw_batch(batch_id: int, payload: WithdrawPayload) -> dict[str, Any]:
    """撤回批次发布，回放回落到仍可读的原载荷。"""
    return _ok(get_migration_service().withdraw_batch(batch_id, payload.reason))


@router.get("/migration/batches/{batch_id}")
def migration_detail(batch_id: int) -> dict[str, Any]:
    """迁移明细：消息身份、分类键、新旧版本与原文留存。"""
    return _ok(get_migration_service().migration_detail(batch_id))


@router.get("/migration/batches")
def list_batches(source: str | None = None) -> dict[str, Any]:
    service = get_migration_service()
    items = service.store.list_batches(source)
    return {"items": items, "total": len(items)}


class CutoverCheckPayload(BaseModel):
    source: str
    sample_message_ids: list[str]


@router.post("/migration/pre-cutover-check")
def pre_cutover_check(payload: CutoverCheckPayload) -> dict[str, Any]:
    """上线切换前核对存量数据与示例数据。"""
    return _ok(get_migration_service().pre_cutover_check(
        source=payload.source, sample_message_ids=payload.sample_message_ids
    ))


# ---------------------------------------------------------------------------
# 入口二：回放
# ---------------------------------------------------------------------------


@router.post("/replay/open")
def open_replay(payload: ReplayOpenPayload) -> dict[str, Any]:
    """受理回放请求并冻结水位。"""
    return _ok(get_migration_service().open_replay(
        source=payload.source, target_topic=payload.target_topic
    ))


@router.post("/replay/{request_id}/run")
def run_replay(request_id: int, payload: ReplayRunPayload | None = None) -> dict[str, Any]:
    """执行回放或中断后续跑（台账去重，幂等）。"""
    limit = payload.limit if payload is not None else None
    return _ok(get_migration_service().run_replay(request_id, limit=limit))


@router.post("/replay/{request_id}/interrupt")
def interrupt_replay(request_id: int, payload: InterruptPayload) -> dict[str, Any]:
    """中断回放，保留断点。"""
    return _ok(get_migration_service().interrupt_replay(request_id, payload.reason))


@router.post("/replay/{request_id}/resume")
def resume_replay(request_id: int) -> dict[str, Any]:
    """从中断点恢复回放。"""
    return _ok(get_migration_service().resume_replay(request_id))


@router.get("/replay/{request_id}/reconcile")
def reconcile(request_id: int) -> dict[str, Any]:
    """重跑/回滚后核对实际交付与确认范围。"""
    return _ok(get_migration_service().reconcile(request_id))


@router.get("/replay/requests")
def list_replay_requests() -> dict[str, Any]:
    service = get_migration_service()
    items = service.store.list_replay_requests()
    return {"items": items, "total": len(items)}
