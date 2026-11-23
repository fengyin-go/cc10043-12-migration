/**
 * 载荷编码迁移与版本化回放接口封装。
 *
 * 对应后端 /api/migration、/api/ingress、/api/replay 两组入口，
 * 与通用资源路由分开，承载编码版本核验、迁移批次和回放水位等专用语义。
 */
import { request } from './client'

export type SourceInfo = {
  source: string
  registered: boolean
  source_name: string
  origin_format: string | null
  origin_format_name?: string
  note: string
}

export type StoredVersion = {
  version_id: number
  version_no: number
  seq: number
  source: string
  message_id: string
  enc: string
  published: boolean
  envelope: Record<string, unknown> | null
  raw_text: string
  note: string
}

export type MigrationItem = {
  item_id: number
  source: string
  message_id: string
  classification_key: string
  origin_format: string | null
  convertible: boolean
  reason: string
  raw_text: string
  legacy_version_id: number
  new_version_id: number | null
  new_enc: string | null
  data_sha256: string | null
}

export type MigrationBatch = {
  batch_id: number
  source: string
  status: string
  scope_note: string
  item_count: number
  convertible_count: number
  unconvertible_count: number
  created_at: string
  published_at: string | null
  withdrawn_at: string | null
}

export type ReplayRequest = {
  request_id: number
  source: string
  target_topic: string
  cutoff_seq: number
  status: string
  confirmed_seq: number
  delivered_count: number
}

async function postJson<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export async function listSources(): Promise<{
  origin_formats: Record<string, string>
  sources: SourceInfo[]
}> {
  return getJson('/api/migration/sources')
}

export async function loadLegacy(
  source: string,
  messageId: string,
  rawText: string,
): Promise<Record<string, unknown>> {
  return postJson('/api/migration/legacy/load', {
    source,
    message_id: messageId,
    raw_text: rawText,
  })
}

export async function listStoredVersions(
  source?: string,
): Promise<{ items: StoredVersion[]; total: number }> {
  const query = source ? `?source=${encodeURIComponent(source)}` : ''
  return getJson(`/api/migration/versions${query}`)
}

export async function ingressJson(
  source: string,
  rawJsonText: string,
): Promise<Record<string, unknown>> {
  return postJson('/api/ingress/json', { source, raw_json_text: rawJsonText })
}

export async function prepareMigration(
  source: string,
  messageIds?: string[],
  scopeNote = '',
): Promise<Record<string, any>> {
  return postJson('/api/migration/prepare', {
    source,
    message_ids: messageIds ?? null,
    scope_note: scopeNote,
  })
}

export async function verifyBatch(batchId: number): Promise<Record<string, any>> {
  return postJson(`/api/migration/batches/${batchId}/verify`)
}

export async function publishBatch(batchId: number): Promise<Record<string, any>> {
  return postJson(`/api/migration/batches/${batchId}/publish`)
}

export async function withdrawBatch(
  batchId: number,
  reason: string,
): Promise<Record<string, any>> {
  return postJson(`/api/migration/batches/${batchId}/withdraw`, { reason })
}

export async function getBatchDetail(
  batchId: number,
): Promise<{ ok: boolean; batch: MigrationBatch; items: MigrationItem[] }> {
  return getJson(`/api/migration/batches/${batchId}`)
}

export async function listBatches(
  source?: string,
): Promise<{ items: MigrationBatch[]; total: number }> {
  const query = source ? `?source=${encodeURIComponent(source)}` : ''
  return getJson(`/api/migration/batches${query}`)
}

export async function preCutoverCheck(
  source: string,
  sampleMessageIds: string[],
): Promise<Record<string, any>> {
  return postJson('/api/migration/pre-cutover-check', {
    source,
    sample_message_ids: sampleMessageIds,
  })
}

export async function openReplay(
  source: string,
  targetTopic: string,
): Promise<Record<string, any>> {
  return postJson('/api/replay/open', { source, target_topic: targetTopic })
}

export async function runReplay(
  requestId: number,
  limit?: number,
): Promise<Record<string, any>> {
  return postJson(`/api/replay/${requestId}/run`, { limit: limit ?? null })
}

export async function interruptReplay(
  requestId: number,
  reason: string,
): Promise<Record<string, any>> {
  return postJson(`/api/replay/${requestId}/interrupt`, { reason })
}

export async function resumeReplay(requestId: number): Promise<Record<string, any>> {
  return postJson(`/api/replay/${requestId}/resume`)
}

export async function reconcile(requestId: number): Promise<Record<string, any>> {
  return getJson(`/api/replay/${requestId}/reconcile`)
}

export async function listReplayRequests(): Promise<{
  items: ReplayRequest[]
  total: number
}> {
  return getJson('/api/replay/requests')
}
