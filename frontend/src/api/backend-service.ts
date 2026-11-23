import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type JsonRecord = Record<string, unknown>

export async function fetchJsonRecord<T = JsonRecord>(path: string): Promise<T> {
  const response = await request(path)
  const payload = await response.json()
  if (!response.ok) {
    throw new Error(payload?.detail ?? `接口返回 ${response.status}`)
  }
  return payload as T
}

/** Schema变更的回退依据：具体变更、备份列和目录确认状态。 */
export function loadRollbackEvidence(changeId: number) {
  return fetchJsonRecord(`/api/schema_change/${changeId}/rollback-evidence`)
}

/** 目录待处理清单里的回退项。 */
export function loadPendingRollbacks(syncId: number) {
  return fetchJsonRecord(`/api/catalog_sync/${syncId}/pending-rollbacks`)
}

export async function confirmCatalogRollback(syncId: number, changeId: number): Promise<ActionResult> {
  const response = await request(`/api/catalog_sync/${syncId}/confirm-rollback`, {
    method: 'POST',
    body: JSON.stringify({ change_id: changeId }),
  })
  const payload = await response.json()
  return { ok: Boolean(payload.ok), message: payload.message ?? '目录确认失败' }
}

export async function applyLateSync(
  syncId: number,
  table: string,
  sourceColumns: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/catalog_sync/${syncId}/apply-schema`, {
    method: 'POST',
    body: JSON.stringify({ table, source_columns: sourceColumns }),
  })
  const payload = await response.json()
  return { ok: Boolean(payload.ok), message: payload.message ?? '同步落地失败' }
}

/** 报表读路径：金额合计（回退办理中返回回退状态）。 */
export function loadAmountSum(table: string) {
  return fetchJsonRecord(`/api/report/amount-sum?table=${encodeURIComponent(table)}`)
}

/** 报表读路径：渠道数据。 */
export function loadChannelData(table: string) {
  return fetchJsonRecord(`/api/report/channel-data?table=${encodeURIComponent(table)}`)
}

/** 报表读路径实际使用的列定义。 */
export function loadColumnDefinitions(table: string) {
  return fetchJsonRecord(`/api/report/column-definitions?table=${encodeURIComponent(table)}`)
}

/** 目录与血缘里的列定义版本和可见性。 */
export function loadSyncUsage(syncId: number, table: string) {
  return fetchJsonRecord(`/api/catalog_sync/${syncId}/sync-usage?table=${encodeURIComponent(table)}`)
}
