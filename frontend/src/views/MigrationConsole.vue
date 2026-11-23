<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>载荷编码迁移控制台</h2>
        <p class="page-desc">
          把连接器直写的 Python 字面量（True/None）迁移为带版本标识的 JSON 信封；
          接入与回放两个入口共享同一版本仓，迁移批次可发布、可撤回。
        </p>
      </div>
      <button class="btn" @click="refreshAll">刷新全部</button>
    </header>

    <p v-if="error" class="error-text">{{ error }}</p>

    <!-- 区段一：来源登记与原写入格式 -->
    <h3>① 接入来源与原写入格式核验依据</h3>
    <table class="data-table">
      <thead>
        <tr><th>来源编码</th><th>来源名称</th><th>原写入格式</th><th>说明</th></tr>
      </thead>
      <tbody>
        <tr v-for="src in sources" :key="src.source">
          <td>{{ src.source }}</td>
          <td>{{ src.source_name }}</td>
          <td>{{ src.origin_format ?? '—（未登记）' }}</td>
          <td>{{ src.note }}</td>
        </tr>
      </tbody>
    </table>

    <!-- 区段二：存量装载与新接入 -->
    <h3>② 存量装载 / 新接收入口</h3>
    <div class="analysis-form">
      <label>来源
        <select v-model="formSource">
          <option v-for="src in sources" :key="src.source" :value="src.source">{{ src.source }}</option>
        </select>
      </label>
      <label>消息ID
        <input v-model="formMessageId" placeholder="如 MSG-1001" />
      </label>
      <label style="flex: 1">原始载荷 / JSON 信封
        <input v-model="formRaw" :placeholder="legacyPlaceholder" style="min-width: 320px" />
      </label>
      <button class="btn" @click="onLoadLegacy">装载存量字面量</button>
      <button class="btn primary" @click="onIngress">新接入 JSON</button>
    </div>

    <!-- 区段三：迁移批次 -->
    <h3>③ 迁移批次（预备 → 核对 → 发布 / 撤回）</h3>
    <div class="analysis-form">
      <label>迁移来源
        <select v-model="batchSource">
          <option v-for="src in sources" :key="src.source" :value="src.source">{{ src.source }}</option>
        </select>
      </label>
      <label>范围说明
        <input v-model="batchNote" placeholder="可选，默认按来源全量存量" />
      </label>
      <button class="btn primary" @click="onPrepare">预备迁移批次</button>
      <button class="btn" @click="onCutoverCheck">切换前核对（示例消息）</button>
    </div>
    <table class="data-table">
      <thead>
        <tr>
          <th>批次</th><th>来源</th><th>状态</th><th>可转换/保留</th>
          <th>发布时间</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="batch in batches" :key="batch.batch_id">
          <td>#{{ batch.batch_id }}</td>
          <td>{{ batch.source }}</td>
          <td>{{ batch.status }}</td>
          <td>{{ batch.convertible_count }} / {{ batch.unconvertible_count }}</td>
          <td>{{ batch.published_at ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" @click="onShowDetail(batch.batch_id)">明细</button>
            <button class="link" @click="onVerify(batch.batch_id)">核对</button>
            <button
              v-if="batch.status === '待发布'"
              class="link"
              @click="onPublish(batch.batch_id)"
            >发布</button>
            <button
              v-if="batch.status === '已发布'"
              class="link"
              @click="onWithdraw(batch.batch_id)"
            >撤回</button>
          </td>
        </tr>
        <tr v-if="batches.length === 0"><td colspan="6" class="empty-state">暂无迁移批次</td></tr>
      </tbody>
    </table>

    <!-- 区段四：回放入口 -->
    <h3>④ 回放（受理冻结水位，按可核验版本读取 JSON 或原载荷）</h3>
    <div class="analysis-form">
      <label>回放来源
        <select v-model="replaySource">
          <option v-for="src in sources" :key="src.source" :value="src.source">{{ src.source }}</option>
        </select>
      </label>
      <label>目标 Topic
        <input v-model="replayTopic" placeholder="topic.order.replay" />
      </label>
      <button class="btn primary" @click="onOpenReplay">受理回放（冻结水位）</button>
    </div>
    <table class="data-table">
      <thead>
        <tr>
          <th>请求</th><th>来源</th><th>状态</th><th>受理水位</th>
          <th>确认序号</th><th>已交付</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="req in replayRequests" :key="req.request_id">
          <td>#{{ req.request_id }}</td>
          <td>{{ req.source }}</td>
          <td>{{ req.status }}</td>
          <td>{{ req.cutoff_seq }}</td>
          <td>{{ req.confirmed_seq }}</td>
          <td>{{ req.delivered_count }}</td>
          <td class="row-actions">
            <button class="link" @click="onRunReplay(req.request_id)">执行/续跑</button>
            <button class="link" @click="onInterrupt(req.request_id)">中断</button>
            <button class="link" @click="onReconcile(req.request_id)">对账</button>
          </td>
        </tr>
        <tr v-if="replayRequests.length === 0">
          <td colspan="7" class="empty-state">暂无回放请求</td>
        </tr>
      </tbody>
    </table>

    <!-- 当前可见版本 -->
    <h3>⑤ 当前各消息可见版本与数据</h3>
    <table class="data-table">
      <thead>
        <tr><th>来源</th><th>消息ID</th><th>编码版本</th><th>版本号</th><th>发布</th><th>序号</th><th>数据/原文摘要</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in versions" :key="`${row.source}-${row.message_id}-${row.version_id}`">
          <td>{{ row.source }}</td>
          <td>{{ row.message_id }}</td>
          <td>{{ row.enc }}</td>
          <td>v{{ row.version_no }}</td>
          <td>{{ row.published ? '是' : '否' }}</td>
          <td>{{ row.seq }}</td>
          <td class="raw-cell">{{ summarize(row) }}</td>
        </tr>
        <tr v-if="versions.length === 0"><td colspan="7" class="empty-state">版本仓暂无数据</td></tr>
      </tbody>
    </table>

    <div v-if="lastResult" class="result-panel">
      <div class="result-head">
        <strong>最近一次操作结果</strong>
        <button class="btn ghost" @click="lastResult = null">关闭</button>
      </div>
      <pre>{{ JSON.stringify(lastResult, null, 2) }}</pre>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  getBatchDetail,
  ingressJson,
  interruptReplay,
  listBatches,
  listReplayRequests,
  listSources,
  listStoredVersions,
  loadLegacy,
  openReplay,
  preCutoverCheck,
  prepareMigration,
  publishBatch,
  reconcile,
  resumeReplay,
  runReplay,
  verifyBatch,
  withdrawBatch,
  type MigrationBatch,
  type ReplayRequest,
  type SourceInfo,
  type StoredVersion,
} from '@/api/migration-service'

const sources = ref<SourceInfo[]>([])
const batches = ref<MigrationBatch[]>([])
const replayRequests = ref<ReplayRequest[]>([])
const versions = ref<StoredVersion[]>([])
const lastResult = ref<unknown>(null)
const error = ref('')

const formSource = ref('src.order_connector_a')
const formMessageId = ref('')
const formRaw = ref('')
const batchSource = ref('src.order_connector_a')
const batchNote = ref('')
const replaySource = ref('src.order_connector_a')
const replayTopic = ref('topic.order.replay')

const legacyPlaceholder =
  "存量：{'order_id': 'ORD-1', 'paid': True, 'coupon': None} ；新接入：带 enc 字段的 JSON 信封"

async function guard<T>(fn: () => Promise<T>): Promise<T | undefined> {
  error.value = ''
  try {
    const result = await fn()
    lastResult.value = result
    return result
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    return undefined
  }
}

async function refreshAll() {
  const [sourceData, batchData, replayData, versionData] = await Promise.all([
    listSources(),
    listBatches(),
    listReplayRequests(),
    listStoredVersions(),
  ])
  sources.value = sourceData.sources
  batches.value = batchData.items
  replayRequests.value = replayData.items
  versions.value = versionData.items
}

async function onLoadLegacy() {
  await guard(() => loadLegacy(formSource.value, formMessageId.value, formRaw.value))
  await refreshAll()
}

async function onIngress() {
  await guard(() => ingressJson(formSource.value, formRaw.value))
  await refreshAll()
}

async function onPrepare() {
  await guard(() => prepareMigration(batchSource.value, undefined, batchNote.value))
  await refreshAll()
}

async function onVerify(batchId: number) {
  await guard(() => verifyBatch(batchId))
}

async function onPublish(batchId: number) {
  await guard(() => publishBatch(batchId))
  await refreshAll()
}

async function onWithdraw(batchId: number) {
  const reason = window.prompt('撤回原因', '切换核对失败，撤回当前批次')
  if (!reason) return
  await guard(() => withdrawBatch(batchId, reason))
  await refreshAll()
}

async function onShowDetail(batchId: number) {
  await guard(() => getBatchDetail(batchId))
}

async function onCutoverCheck() {
  const sample = window.prompt('示例消息ID（逗号分隔）', 'MSG-A1')
  if (!sample) return
  const ids = sample.split(',').map((item) => item.trim()).filter(Boolean)
  await guard(() => preCutoverCheck(batchSource.value, ids))
}

async function onOpenReplay() {
  await guard(() => openReplay(replaySource.value, replayTopic.value))
  await refreshAll()
}

async function onRunReplay(requestId: number) {
  // 已中断的请求走续跑，其余直接执行；后端台账保证幂等。
  const target = replayRequests.value.find((item) => item.request_id === requestId)
  await guard(() =>
    target?.status === '回放中断' ? resumeReplay(requestId) : runReplay(requestId),
  )
  await refreshAll()
}

async function onInterrupt(requestId: number) {
  await guard(() => interruptReplay(requestId, '控制台手动中断'))
  await refreshAll()
}

async function onReconcile(requestId: number) {
  await guard(() => reconcile(requestId))
}

function summarize(row: StoredVersion): string {
  if (row.envelope?.data !== undefined) {
    return JSON.stringify(row.envelope.data)
  }
  return row.raw_text.length > 80 ? `${row.raw_text.slice(0, 80)}…` : row.raw_text
}

onMounted(refreshAll)
</script>

<style scoped>
h3 {
  margin: 20px 0 8px;
  font-size: 15px;
}
.raw-cell {
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.result-panel {
  margin-top: 16px;
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 8px;
  padding: 12px;
}
.result-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.result-panel pre {
  max-height: 320px;
  overflow: auto;
  font-size: 12px;
  margin: 0;
}
</style>
