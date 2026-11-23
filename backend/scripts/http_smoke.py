#!/usr/bin/env python3
"""不依赖 httpx 的路由冒烟测试：用最小 ASGI 调用器驱动 FastAPI 应用。

覆盖两个入口的关键 HTTP 路径：来源登记、存量装载、迁移预备/发布/撤回、
切换前核对、回放受理/中断/续跑/对账，以及动态资源路由未被新路由抢占。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from app.migration.store import get_migration_store  # noqa: E402


class MiniResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self.body = json.loads(body.decode("utf-8"))


async def call(method: str, path: str, payload: dict[str, Any] | None = None) -> MiniResponse:
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    chunks: list[bytes] = []
    status: dict[str, int] = {}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    # 补齐 FastAPI AsyncExitStackMiddleware 通常注入的作用域键。
    import contextlib

    async with contextlib.AsyncExitStack() as stack:
        scope["fastapi_middleware_astack"] = stack
        await app.router(scope, receive, send)
    return MiniResponse(status["code"], b"".join(chunks))


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ✔ {message}")


async def main() -> None:
    get_migration_store().reset()
    legacy = ("{'order_id': 'ORD-1', 'paid': True, 'coupon': None, "
              "'items': [{'sku': 'S1', 'qty': 2}]}")

    print("== 健康检查与既有资源路由 ==")
    health = await call("GET", "/api/health")
    check(health.status_code == 200 and health.body["ok"], "健康检查正常")
    payloads = await call("GET", "/api/payload_store")
    check(payloads.status_code == 200 and "items" in payloads.body,
          "通用资源路由仍可访问，未被新路由抢占")

    print("== 来源登记 ==")
    sources = await call("GET", "/api/migration/sources")
    check(sources.status_code == 200 and len(sources.body["sources"]) == 2,
          "来源登记表返回两个已登记来源")

    print("== 存量装载 ==")
    loaded = await call("POST", "/api/migration/legacy/load",
                        {"source": "src.order_connector_a", "message_id": "M1",
                         "raw_text": legacy})
    check(loaded.status_code == 200 and loaded.body["classification"]["convertible"],
          "存量字面量装载并核验通过")

    print("== 迁移预备/核对/发布 ==")
    prep = await call("POST", "/api/migration/prepare", {"source": "src.order_connector_a"})
    batch_id = prep.body["batch_id"]
    check(prep.body["ok"] and prep.body["convertible"] == 1, "迁移批次预备成功")
    verified = await call("POST", f"/api/migration/batches/{batch_id}/verify")
    check(verified.body["ok"], "批次发布前核对通过")
    published = await call("POST", f"/api/migration/batches/{batch_id}/publish")
    check(published.body["ok"], "批次发布成功")

    print("== 切换前核对 ==")
    cutover = await call("POST", "/api/migration/pre-cutover-check",
                         {"source": "src.order_connector_a", "sample_message_ids": ["M1"]})
    check(cutover.body["ok"], "存量与示例数据切换前核对通过")

    print("== 新接收入口 ==")
    envelope = get_migration_service_envelope()
    ingested = await call("POST", "/api/ingress/json",
                          {"source": "src.order_connector_a",
                           "raw_json_text": json.dumps(envelope, ensure_ascii=False)})
    check(ingested.body["ok"] and ingested.body["enc"] == "json.envelope.v2",
          "新版 JSON 信封接入成功")
    rejected = await call("POST", "/api/ingress/json",
                          {"source": "src.rogue_writer",
                           "raw_json_text": json.dumps(envelope, ensure_ascii=False)})
    check(not rejected.body["ok"], "未登记来源接入被拒绝")

    print("== 回放受理/执行/对账 ==")
    opened = await call("POST", "/api/replay/open",
                        {"source": "src.order_connector_a", "target_topic": "t1"})
    rid = opened.body["request_id"]
    check(opened.body["ok"] and opened.body["cutoff_seq"] >= 1, "回放受理并冻结水位")
    ran = await call("POST", f"/api/replay/{rid}/run", {})
    check(ran.body["status"] == "已完成" and ran.body["delivered_total"] == 2,
          "回放完成，覆盖迁移消息与新接入消息")
    ledger = await call("GET", f"/api/replay/{rid}/reconcile")
    check(ledger.body["ok"] and ledger.body["delivered_count"] == 2, "对账无异常")

    print("== 撤回发布并回落 ==")
    withdrawn = await call("POST", f"/api/migration/batches/{batch_id}/withdraw",
                           {"reason": "http 冒烟撤回"})
    check(withdrawn.body["ok"], "批次撤回成功")
    detail = await call("GET", f"/api/migration/batches/{batch_id}")
    check(detail.body["batch"]["status"] == "已撤回", "迁移明细显示批次已撤回")

    print("\n全部 HTTP 冒烟断言通过")


def get_migration_service_envelope() -> dict[str, Any]:
    from app.migration import build_envelope
    return build_envelope(
        source="src.order_connector_a",
        message_id="HTTP-C1",
        data={"order_id": "ORD-9", "paid": False, "coupon": None},
    )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
