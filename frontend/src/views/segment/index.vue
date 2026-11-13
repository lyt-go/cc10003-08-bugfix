<template>
  <section class="page" data-module="segment">
    <header class="page-head">
      <div>
        <h2>分群分析管理</h2>
        <p class="page-desc">定义用户或业务对象的分群规则，规模边界与标签版本共同决定分群能否生效。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记人群分群</button>
        <button class="btn" type="button" @click="exportRows">导出分群分析清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-abnormal': row.abnormal }">
          <td v-for="column in columns" :key="column">
            <span v-if="column === '最终规模' && row[column] === null" class="muted">未刷新</span>
            <span v-else-if="column === '标签版本' && row[column] === null" class="muted">—</span>
            <span v-else>{{ row[column] ?? '—' }}</span>
          </td>
          <td>
            <div class="status-cell">
              <span class="status-tag" :class="statusClass(row.status)">{{ row.status }}</span>
              <span class="refresh-tag" v-if="row.刷新状态">{{ row.刷新状态 }}</span>
              <p v-if="row.校验原因" class="reject-reason" :title="String(row.校验原因)">{{ row.校验原因 }}</p>
            </div>
          </td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openEdit(row)">编辑</button>
            <button
              class="link"
              type="button"
              :disabled="row.刷新状态 === '计算中'"
              @click="startRefresh(row)"
            >
              刷新计算
            </button>
            <button class="link" type="button" @click="openAudit(row)">审计</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无分群分析数据，可先登记人群分群</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条分群分析记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 编辑/登记弹窗 -->
    <div v-if="editing" class="modal-mask" @click.self="editing = null">
      <div class="modal">
        <h3>{{ editing.id ? '编辑分群配置' : '登记人群分群' }}</h3>
        <label v-for="field in formFields" :key="field" class="form-item">
          <span>{{ field }}</span>
          <input v-model="editing[field]" :placeholder="`请输入${field}`" />
        </label>
        <p v-if="formError" class="error-text">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="editing = null">取消</button>
          <button class="btn primary" type="button" @click="saveEditing">保存</button>
        </div>
      </div>
    </div>

    <!-- 审计抽屉 -->
    <div v-if="auditing" class="modal-mask" @click.self="auditing = null">
      <div class="modal modal-wide">
        <h3>审计台账 · {{ auditing.分群编号 }}</h3>
        <table class="data-table">
          <thead>
            <tr><th>时间</th><th>动作</th><th>结果</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in auditRows" :key="item.id">
              <td>{{ item.时间 }}</td>
              <td>{{ item.动作 }}</td>
              <td :class="item.结果 === '拒绝' ? 'error-text' : ''">{{ item.结果 }}</td>
              <td>{{ item.说明 }}</td>
            </tr>
            <tr v-if="!auditRows.length">
              <td colspan="4" class="empty-state">暂无审计记录</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn primary" type="button" @click="auditing = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
type EditingRow = Record<string, any> | null

const ENDPOINT = '/api/segment'
const columns = ['分群编号', '分群名称', '筛选规则', '关联标签编号', '标签版本', '最小规模', '最大规模', '最终规模', '最近计算时间']
const formFields = ['分群编号', '分群名称', '筛选规则', '关联标签编号', '最小规模', '最大规模', '刷新周期']
const statuses = ['草稿', '规则校验中', '已生效', '已失效']
const stats = ref([
  { label: '人群分群总数', value: 0 },
  { label: '已生效分群', value: 0 },
  { label: '规则校验中', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const editing = ref<EditingRow>(null)
const formError = ref('')
const auditing = ref<Row | null>(null)
const auditRows = ref<Row[]>([])

const ACTIONS_BY_STATUS: Record<string, string[]> = {
  草稿: ['提交规则校验'],
  规则校验中: ['生效分群'],
  已生效: ['失效分群'],
  已失效: ['提交规则校验'],
}

function availableActions(row: Row): string[] {
  return ACTIONS_BY_STATUS[String(row.status)] ?? []
}

function statusClass(status: string | number | null): string {
  return {
    草稿: 'status-draft',
    规则校验中: 'status-checking',
    已生效: 'status-active',
    已失效: 'status-expired',
  }[String(status)] ?? ''
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  formError.value = ''
  editing.value = {
    分群编号: '',
    分群名称: '',
    筛选规则: '',
    关联标签编号: '',
    最小规模: '',
    最大规模: '',
    刷新周期: '每日',
  }
}

function openEdit(row: Row) {
  formError.value = ''
  editing.value = { ...row }
}

async function saveEditing() {
  if (!editing.value) return
  const target = editing.value
  const isCreate = !target.id
  try {
    const response = await request(isCreate ? ENDPOINT : `${ENDPOINT}/${target.id}`, {
      method: isCreate ? 'POST' : 'PUT',
      body: JSON.stringify({ values: target }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      formError.value = payload.message
      return
    }
    editing.value = null
    await reload()
  } catch (error) {
    formError.value = error instanceof Error ? error.message : '保存失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      // 拒绝原因由后端给出，直接展示；列表重新拉取后校验原因仍持久可见
      errorMessage.value = payload.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分群分析操作失败'
  }
}

async function startRefresh(row: Row) {
  errorMessage.value = ''
  try {
    const createResp = await request(`${ENDPOINT}/${row.id}/refresh`, { method: 'POST' })
    const created = await createResp.json()
    if (!created.ok) {
      errorMessage.value = created.message
      await reload()
      return
    }
    const batch = Number(created.entry.刷新批次)
    // 演示环境：弹窗收集本次覆盖计算结果，真实场景由计算引擎回调 complete 接口
    const input = window.prompt(`刷新批次 ${batch} 已创建，请输入本次覆盖对象数（输入 fail 模拟计算失败）：`, '260')
    if (input === null) {
      await reload()
      return
    }
    let body: Record<string, unknown>
    if (input.trim().toLowerCase() === 'fail') {
      body = { batch, failed: true, failure_reason: '人工模拟计算引擎失败' }
    } else {
      body = { batch, coverage: Number(input) }
    }
    const completeResp = await request(`${ENDPOINT}/${row.id}/refresh/complete`, {
      method: 'POST',
      body: JSON.stringify({ values: body }),
    })
    const completed = await completeResp.json()
    if (!completed.ok) {
      errorMessage.value = completed.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新计算失败'
  }
}

async function openAudit(row: Row) {
  auditing.value = row
  auditRows.value = []
  try {
    const response = await request(`${ENDPOINT}/${row.id}/audit`)
    const payload = await response.json()
    auditRows.value = payload.items ?? []
  } catch {
    errorMessage.value = '审计台账读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('人群分群列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((r) => r.status === '已生效').length
    stats.value[2].value = rows.value.filter((r) => r.status === '规则校验中').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分群分析列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.muted { color: var(--muted); }
.row-abnormal { background: #fff8f7; }
.status-cell { display: flex; flex-direction: column; gap: 4px; }
.status-tag, .refresh-tag {
  display: inline-block; width: fit-content; padding: 2px 8px;
  border-radius: 10px; font-size: 12px; border: 1px solid var(--border);
}
.status-draft { background: #f1f5f9; color: #475569; }
.status-checking { background: #fef3c7; color: #92400e; border-color: #fcd34d; }
.status-active { background: #dcfce7; color: #166534; border-color: #86efac; }
.status-expired { background: #fee2e2; color: #991b1b; border-color: #fca5a5; }
.refresh-tag { background: #eef2ff; color: #3730a3; border-color: #c7d2fe; }
.reject-reason {
  margin: 0; max-width: 260px; font-size: 12px; color: #b42318;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.modal-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 20;
}
.modal {
  background: #fff; border-radius: 10px; padding: 20px; width: 460px;
  max-height: 80vh; overflow: auto;
}
.modal-wide { width: 760px; }
.form-item { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; font-size: 13px; }
.form-item input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
button:disabled { color: #94a3b8; cursor: not-allowed; }
</style>
