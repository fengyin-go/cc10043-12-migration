# API数据接入与消息流处理平台

面向第三方API和消息队列的数据接入、流式处理、水位管理和断点续采的采集治理平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8017/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5197/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8017`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API连接器 | `api_connector` | API连接器 | 连接器名称、API端点、认证方式 |
| 消息流源 | `stream_source` | 消息流源 | 源名称、中间件类型、Topic/Queue名 |
| 偏移量管理 | `offset_manager` | 偏移量记录 | 消费组、Topic/Queue、分区/分片 |
| API调用调度 | `api_scheduler` | 调用计划 | 计划名称、关联API、Cron表达式 |
| 记录检查点 | `record_checkpoint` | 检查点 | 关联接入任务、检查点类型、检查点值 |
| 消息解析器 | `message_parser` | 解析模板 | 模板名称、消息格式、字段映射 |
| 去重中心 | `dedup_center` | 去重键 | 去重键名、关联数据源、去重窗口秒数 |
| 水位追踪 | `watermark_tracker` | 水位记录 | 数据源、水位字段、当前水位 |
| 频控保护 | `rate_guard` | 频控规则 | 规则名称、限流维度、每秒上限 |
| 失败隔离区 | `failure_isolation` | 隔离记录 | 失败消息ID、失败原因、原始Topic |
| 流数据处理审计 | `stream_audit` | 审计明细 | 处理时间、数据源、消息总数 |
| 批量消息回放 | `batch_replay` | 回放任务 | 回放范围、开始时间、结束时间 |
| 字段转换器 | `field_transform` | 转换规则 | 规则名称、源字段、目标字段 |
| API调用日志 | `api_logger` | 调用日志 | 调用时间、API名称、请求参数摘要 |
| 数据落地配置 | `data_sink` | 落地配置 | 配置名称、目标存储类型、写入模式 |
| Schema注册中心 | `schema_registry` | Schema版本 | Schema名称、版本号、字段定义 |
| 流式数据导出 | `stream_export` | 导出任务 | 任务名称、数据源、导出格式 |
| 续采策略 | `resume_policy` | 续采规则 | 规则名称、续采起点类型、续采步长 |
| 消息载荷存储 | `payload_store` | 原始载荷 | 采集批次、消息ID、原始载荷 |
| 吞吐量告警 | `throughput_alarm` | 告警规则 | 规则名称、监控数据源、吞吐量下限 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 载荷编码迁移与版本化回放（`backend/app/migration/`）

把连接器直写的 Python 字面量（历史订单保留 `True` / `None`）迁移为带版本标识的
JSON 信封 `json.envelope.v2`，接入与回放两个入口共用一个追加式版本仓。

- `registry.py`：接入来源与原写入格式登记表。旧载荷按**来源 + 原写入格式**核验，
  文本相同但来源不同的载荷分类键不同、不会归并；来源不明保留原文并标记无法转换。
- `codec.py`：`ast.literal_eval` 安全解析旧字面量（拒绝任何调用表达式）；信封带
  `enc` 编码版本与 `data_sha256` 哈希，读取时先验版本再验哈希，篡改即拒绝。
- `store.py`：线程安全的追加式版本仓。迁移只追加新版本、不改写旧版本；每条版本带
  单调 `seq`，回放请求受理时冻结 `cutoff_seq` 水位。
- `service.py`：迁移批次（预备 → 切换前核对 → 发布 / 撤回）与回放编排（受理冻结、
  中断续跑、重跑幂等、回滚对账）。撤回只翻发布位，回放回落到仍可读的 v1 原载荷。
- 前端「载荷迁移控制台」（导航第二项，路由 `/migration-console`）覆盖两个入口的
  全部操作与版本查询。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/migration/sources` | 来源登记与原写入格式 |
| POST | `/api/migration/legacy/load` | 装载存量 Python 字面量载荷 |
| POST | `/api/ingress/json` | 新接收入口（仅接受通过核验的 v2 信封） |
| POST | `/api/migration/prepare` | 预备迁移批次（生成未发布 v2 与核验明细） |
| POST | `/api/migration/batches/{id}/verify` | 批次发布前核对（版本/哈希/原文重验） |
| POST | `/api/migration/batches/{id}/publish` | 发布批次（内部先核对，失败不发布） |
| POST | `/api/migration/batches/{id}/withdraw` | 撤回当前批次发布 |
| POST | `/api/migration/pre-cutover-check` | 切换前核对存量数据与示例数据 |
| POST | `/api/replay/open` | 受理回放请求并冻结水位 |
| POST | `/api/replay/{id}/run` | 执行回放（可带 limit 模拟中断点） |
| POST | `/api/replay/{id}/interrupt` `/resume` | 中断与恢复（台账按消息身份去重） |
| GET | `/api/replay/{id}/reconcile` | 重跑/回滚后核对实际交付与确认范围 |
| GET | `/api/migration/versions` | 查询各消息当前可见版本与数据 |

### 验证

```bash
cd backend
python3 -I -m unittest discover -s tests          # 17 个标准库单元测试，含并发冲突
python3 -I scripts/migration_demo.py              # 端到端剧情：同文异源、中断插入 C、恢复隔离、撤回回落
.venv/bin/python scripts/http_smoke.py            # 不依赖 httpx 的路由冒烟（最小 ASGI 调用器）
.venv/bin/python -m uvicorn app.main:app --port 8017
python3 -I scripts/live_scenario.py               # 对运行中服务跑真实 HTTP 场景
```

领域：数据工程
子方向：数据采集、爬虫与RPA
