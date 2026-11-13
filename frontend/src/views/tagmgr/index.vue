<template>
  <section class="page" data-module="tagmgr">
    <header class="page-head">
      <div>
        <h2>标签管理</h2>
        <p class="page-desc">管理业务标签的定义、版本与规则集合；换版发布后引用它的已生效分群将回到规则校验中。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记标签定义</button>
        <button class="btn" type="button" @click="exportRows">导出标签管理清单</button>
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
            <span v-if="column === '覆盖对象数' && row[column] === null" class="muted">未回写</span>
            <span v-else-if="column === '规则集合'">{{ (row[column] as string[] | null)?.join('、') ?? '—' }}</span>
            <span v-else>{{ row[column] ?? '—' }}</span>
          </td>
          <td>
            <span class="status-tag" :class="statusClass(row.status)">{{ row.status }}</span>
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
            <button class="link" type="button" @click="openTrace(row)">版本追溯</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无标签管理数据，可先登记标签定义</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条标签管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 编辑/登记弹窗 -->
    <div v-if="editing" class="modal-mask" @click.self="editing = null">
      <div class="modal">
        <h3>{{ editing.id ? '编辑标签定义' : '登记标签定义' }}</h3>
        <label v-for="field in scalarFields" :key="field" class="form-item">
          <span>{{ field }}</span>
          <input v-model="(editing as Record<string, string>)[field]" :placeholder="`请输入${field}`" />
        </label>
        <label class="form-item">
          <span>规则集合（属性编号，逗号分隔）</span>
          <input v-model="ruleSetText" placeholder="如 DIMP-0001,DIMP-0003" />
        </label>
        <p v-if="editing.id" class="tip">已发布标签编辑后会退回草稿，重新发布即生成新版本。</p>
        <p v-if="formError" class="error-text">{{ formError }}</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="editing = null">取消</button>
          <button class="btn primary" type="button" @click="saveEditing">保存</button>
        </div>
      </div>
    </div>

    <!-- 版本追溯抽屉 -->
    <div v-if="trace" class="modal-mask" @click.self="trace = null">
      <div class="modal modal-wide">
        <h3>版本追溯 · {{ trace.标签.标签编号 }}（当前 v{{ trace.标签.当前版本 }}）</h3>
        <h4>版本快照</h4>
        <table class="data-table">
          <thead>
            <tr><th>版本</th><th>规则集合</th><th>覆盖对象数</th><th>发布时间</th><th>留存时间</th></tr>
          </thead>
          <tbody>
            <tr v-for="snap in trace.版本快照" :key="snap.版本">
              <td>v{{ snap.版本 }}</td>
              <td>{{ snap.规则集合.join('、') }}</td>
              <td>{{ snap.覆盖对象数 ?? '—' }}</td>
              <td>{{ snap.发布时间 ?? '—' }}</td>
              <td>{{ snap.留存时间 }}</td>
            </tr>
            <tr v-if="!trace.版本快照.length">
              <td colspan="5" class="empty-state">暂无历史版本快照</td>
            </tr>
          </tbody>
        </table>
        <h4>引用分群与最终规模</h4>
        <table class="data-table">
          <thead>
            <tr><th>分群编号</th><th>状态</th><th>引用版本</th><th>最终规模</th><th>规模区间</th><th>最近审计</th></tr>
          </thead>
          <tbody>
            <tr v-for="seg in trace.引用分群" :key="seg.id">
              <td>{{ seg.分群编号 }}</td>
              <td>{{ seg.status }}</td>
              <td>v{{ seg.标签版本 ?? '—' }}</td>
              <td>{{ seg.最终规模 ?? '未刷新' }}</td>
              <td>[{{ seg.最小规模 }}, {{ seg.最大规模 }}]</td>
              <td>{{ seg.审计?.[seg.审计.length - 1]?.说明 ?? '—' }}</td>
            </tr>
            <tr v-if="!trace.引用分群.length">
              <td colspan="6" class="empty-state">暂无分群引用该标签</td>
            </tr>
          </tbody>
        </table>
        <h4>标签审计</h4>
        <ul class="audit-list">
          <li v-for="item in trace.标签审计" :key="item.id">
            {{ item.时间 }} · {{ item.动作 }} · {{ item.结果 }} · {{ item.说明 }}
          </li>
          <li v-if="!trace.标签审计.length" class="muted">暂无审计记录</li>
        </ul>
        <div class="modal-actions">
          <button class="btn primary" type="button" @click="trace = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
interface VersionSnapshot {
  版本: number
  规则集合: string[]
  覆盖对象数: number | null
  发布时间: string | null
  留存时间: string
}
interface SegmentTraceRow extends Row {
  审计: Row[]
}
interface TraceView {
  标签: Row
  标签审计: Row[]
  版本快照: VersionSnapshot[]
  引用分群: SegmentTraceRow[]
}

const ENDPOINT = '/api/tagmgr'
const columns = ['标签编号', '标签名称', '标签分类', '计算逻辑', '值类型', '规则集合', '当前版本', '覆盖对象数', '最近计算时间']
const scalarFields = ['标签编号', '标签名称', '标签分类', '计算逻辑', '值类型', '刷新周期']
const stats = ref([
  { label: '标签定义总数', value: 0 },
  { label: '已发布标签', value: 0 },
  { label: '逻辑评审中', value: 0 },
])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const editing = ref<Row | null>(null)
const formError = ref('')
const ruleSetText = ref('')
const trace = ref<TraceView | null>(null)

const ACTIONS_BY_STATUS: Record<string, string[]> = {
  草稿: ['提交逻辑评审', '发布标签'],
  逻辑评审中: ['发布标签'],
  已发布: ['下线标签'],
  已下线: [],
}

function availableActions(row: Row): string[] {
  return ACTIONS_BY_STATUS[String(row.status)] ?? []
}

function statusClass(status: string | number | null): string {
  return {
    草稿: 'status-draft',
    逻辑评审中: 'status-checking',
    已发布: 'status-active',
    已下线: 'status-expired',
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
    标签编号: '',
    标签名称: '',
    标签分类: '',
    计算逻辑: '',
    值类型: '布尔型',
    刷新周期: '每日',
  }
  ruleSetText.value = ''
}

function openEdit(row: Row) {
  formError.value = ''
  editing.value = { ...row }
  ruleSetText.value = Array.isArray(row.规则集合) ? (row.规则集合 as string[]).join(',') : ''
}

async function saveEditing() {
  if (!editing.value) return
  const target = editing.value
  const values: Record<string, unknown> = { ...target }
  values.规则集合 = ruleSetText.value.split(',').map((s) => s.trim()).filter(Boolean)
  const isCreate = !target.id
  try {
    const response = await request(isCreate ? ENDPOINT : `${ENDPOINT}/${target.id}`, {
      method: isCreate ? 'POST' : 'PUT',
      body: JSON.stringify({ values }),
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
      errorMessage.value = payload.message
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签管理操作失败'
  }
}

async function openTrace(row: Row) {
  trace.value = null
  try {
    const response = await request(`${ENDPOINT}/${row.id}/trace`)
    if (!response.ok) {
      throw new Error('版本追溯读取失败')
    }
    trace.value = (await response.json()) as TraceView
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本追溯读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('标签定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((r) => r.status === '已发布').length
    stats.value[2].value = rows.value.filter((r) => r.status === '逻辑评审中').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标签管理列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.muted { color: var(--muted); }
.row-abnormal { background: #fff8f7; }
.status-tag {
  display: inline-block; padding: 2px 8px; border-radius: 10px;
  font-size: 12px; border: 1px solid var(--border);
}
.status-draft { background: #f1f5f9; color: #475569; }
.status-checking { background: #fef3c7; color: #92400e; border-color: #fcd34d; }
.status-active { background: #dcfce7; color: #166534; border-color: #86efac; }
.status-expired { background: #fee2e2; color: #991b1b; border-color: #fca5a5; }
.modal-mask {
  position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
  display: flex; align-items: center; justify-content: center; z-index: 20;
}
.modal {
  background: #fff; border-radius: 10px; padding: 20px; width: 480px;
  max-height: 80vh; overflow: auto;
}
.modal-wide { width: 860px; }
.form-item { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; font-size: 13px; }
.form-item input { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.tip { font-size: 12px; color: var(--muted); }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.audit-list { font-size: 12px; color: #334155; padding-left: 16px; }
.audit-list li { margin-bottom: 4px; }
h4 { margin: 14px 0 6px; font-size: 13px; }
</style>
