<template>
  <section class="rollback-panel" data-testid="rollback-panel">
    <header class="panel-head">
      <div>
        <h3>Schema回退与读取核对</h3>
        <p class="page-desc">
          回退先提交、再由目录确认结案；办理期间被撤销的列不可读，未撤销列照常可读。
        </p>
      </div>
      <span v-if="busy" class="panel-busy">处理中…</span>
    </header>

    <div class="panel-controls">
      <label class="panel-field">
        <span>变更ID</span>
        <input v-model.number="changeId" data-testid="rollback-change-id" type="number" min="1" />
      </label>
      <label class="panel-field">
        <span>目录同步任务ID</span>
        <input v-model.number="syncId" data-testid="rollback-sync-id" type="number" min="1" />
      </label>
      <label class="panel-field">
        <span>目标表</span>
        <input v-model="table" data-testid="rollback-table" />
      </label>
    </div>

    <div class="panel-actions">
      <button class="btn" data-testid="rollback-evidence" type="button" @click="showEvidence">
        查看回退依据
      </button>
      <button class="btn" data-testid="rollback-read-amount" type="button" @click="readAmount">
        读取金额合计
      </button>
      <button class="btn" data-testid="rollback-read-channel" type="button" @click="readChannel">
        读取渠道数据
      </button>
      <button class="btn" data-testid="rollback-read-columns" type="button" @click="readColumns">
        读取列定义
      </button>
      <button class="btn primary" data-testid="rollback-confirm" type="button" @click="confirmRollback">
        目录确认回退
      </button>
      <button class="btn" data-testid="rollback-replay" type="button" @click="replayLateSync">
        重放迟到同步
      </button>
    </div>

    <p v-if="panelError" class="error-text" data-testid="rollback-error">{{ panelError }}</p>

    <div v-if="result" class="panel-result" data-testid="rollback-result">
      <div class="panel-result-head">
        <strong>{{ result.title }}</strong>
        <span :class="['panel-flag', result.ok ? 'flag-ok' : 'flag-warn']">
          {{ result.ok ? '可读' : '不可读·回退中' }}
        </span>
      </div>
      <dl class="panel-kv">
        <template v-for="item in result.items" :key="item.label">
          <dt>{{ item.label }}</dt>
          <dd>{{ item.value }}</dd>
        </template>
      </dl>
      <pre v-if="result.raw" class="panel-raw">{{ result.raw }}</pre>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  applyLateSync,
  confirmCatalogRollback,
  loadAmountSum,
  loadChannelData,
  loadColumnDefinitions,
  loadPendingRollbacks,
  loadRollbackEvidence,
} from '@/api/backend-service'

type PanelItem = { label: string; value: string }
type PanelResult = { title: string; ok: boolean; items: PanelItem[]; raw?: string }

const changeId = ref(4)
const syncId = ref(4)
const table = ref('order_fact_v3')
const busy = ref(false)
const panelError = ref('')
const result = ref<PanelResult | null>(null)

function text(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.length ? value.join('、') : '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

async function run(task: () => Promise<void>) {
  busy.value = true
  panelError.value = ''
  try {
    await task()
  } catch (error) {
    panelError.value = error instanceof Error ? error.message : '操作失败'
  } finally {
    busy.value = false
  }
}

function showEvidence() {
  void run(async () => {
    const evidence = await loadRollbackEvidence(changeId.value) as Record<string, any>
    const backup = (evidence.backup_columns ?? [])[0] ?? {}
    const pending = evidence.catalog_pending ?? []
    result.value = {
      title: `变更 ${changeId.value} 的回退依据`,
      ok: Boolean(evidence.can_close),
      items: [
        { label: '变更内容', value: text(evidence.change?.from) + ' → ' + text(evidence.change?.to) },
        { label: '涉及列', value: text(evidence.change?.column) },
        { label: '备份列', value: text(backup.backup_column) },
        { label: '恢复为', value: text(backup.type) },
        { label: '回退状态', value: text(evidence.entry?.回退状态) },
        { label: '目录待处理', value: text(pending.map((item: any) => `${item.catalog}/${item.column}`)) },
      ],
      raw: JSON.stringify(evidence, null, 2),
    }
  })
}

function readAmount() {
  void run(async () => {
    const payload = await loadAmountSum(table.value) as Record<string, any>
    result.value = {
      title: '报表读取：金额合计',
      ok: Boolean(payload.ok),
      items: [
        { label: '状态', value: text(payload.state ?? '可读') },
        { label: '列定义', value: text(payload.defined_type) },
        { label: '定义来源', value: text(payload.definition_source) },
        { label: '合计', value: text(payload.sum) },
        { label: '说明', value: text(payload.message) },
      ],
      raw: JSON.stringify(payload, null, 2),
    }
  })
}

function readChannel() {
  void run(async () => {
    const payload = await loadChannelData(table.value) as Record<string, any>
    result.value = {
      title: '报表读取：渠道数据',
      ok: Boolean(payload.ok),
      items: [
        { label: '状态', value: text(payload.state ?? '可读') },
        { label: '列定义', value: text(payload.defined_type) },
        { label: '渠道值', value: text(payload.values) },
        { label: '去重渠道', value: text(payload.distinct) },
      ],
      raw: JSON.stringify(payload, null, 2),
    }
  })
}

function readColumns() {
  void run(async () => {
    const payload = await loadColumnDefinitions(table.value) as Record<string, any>
    const columns = (payload.columns ?? []) as Array<Record<string, any>>
    result.value = {
      title: '报表实际使用的列定义',
      ok: columns.every((item) => item.readable),
      items: columns.map((item) => ({
        label: item.column,
        value: `${item.defined_type} / ${item.definition_source}${
          item.rollback_locked ? ' / 已锁定' : ''}${item.readable ? '' : ' / 不可读'}`,
      })),
      raw: JSON.stringify(payload, null, 2),
    }
  })
}

function confirmRollback() {
  void run(async () => {
    const payload = await confirmCatalogRollback(syncId.value, changeId.value)
    const pending = await loadPendingRollbacks(syncId.value) as Record<string, any>
    result.value = {
      title: '目录确认回退',
      ok: payload.ok,
      items: [
        { label: '结果', value: payload.message },
        { label: '剩余待处理', value: text(pending.pending_count) },
      ],
      raw: JSON.stringify(pending, null, 2),
    }
  })
}

function replayLateSync() {
  void run(async () => {
    const columns = await loadColumnDefinitions(table.value) as Record<string, any>
    const current = Object.fromEntries(
      ((columns.columns ?? []) as Array<Record<string, any>>).map((item) => [item.column, item.defined_type]),
    )
    // 迟到的同步携带改型后的旧定义：金额仍是 STRING。
    const late = { ...current, amount: 'STRING' }
    const payload = await applyLateSync(syncId.value, table.value, late)
    const amount = await loadAmountSum(table.value) as Record<string, any>
    const channel = await loadChannelData(table.value) as Record<string, any>
    const usage = await loadColumnDefinitions(table.value) as Record<string, any>
    result.value = {
      title: '重放迟到同步',
      ok: payload.ok,
      items: [
        { label: '同步落地', value: payload.message },
        { label: '金额合计', value: text(amount.sum) },
        { label: '金额列定义', value: `${text(amount.defined_type)} / ${text(amount.definition_source)}` },
        { label: '渠道数据', value: text(channel.values) },
        { label: '渠道列定义', value: text(channel.defined_type) },
      ],
      raw: JSON.stringify(usage, null, 2),
    }
  })
}
</script>
