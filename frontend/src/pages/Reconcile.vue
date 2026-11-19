<template>
  <div>
    <div class="hero">
      <div>
        <h1>三联对账</h1>
        <p>按供应商+货号+到货日对齐三数量（不计已作废行）；不一致进差额。未决差额清零后可结案。作废会使当日未结案批次失效，须重跑。</p>
      </div>
    </div>
    <div class="panel">
      <div class="row">
        <input v-model="day" placeholder="到货日" />
        <button @click="run">跑三联对账</button>
        <button class="warn" @click="seal">结案封印</button>
        <button class="secondary" @click="load">刷新</button>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
      <p v-if="batch">
        批次 {{ batch.batch_no }} · 对齐 {{ batch.matched_count }} · 差额 {{ batch.variance_count }} · 作废快照 {{ batch.void_count }} ·
        <span class="tag" :class="statusClass">{{ statusText }}</span>
      </p>
      <p v-if="batch && batch.status === 'stale'" class="msg">该批次因作废已失效，仅作历史留存；请重新跑三联对账后再结案。</p>
    </div>
    <div class="panel">
      <h2>对齐行</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>预报</th><th>实收</th><th>回执</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="r in matches" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td><td>{{ r.expect_qty }}</td><td>{{ r.receive_qty }}</td><td>{{ r.confirm_qty }}</td>
            <td><span class="tag" :class="r.aligned ? 'ok' : 'bad'">{{ r.aligned ? '对齐' : '差额' }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="panel">
      <h2>差额处置</h2>
      <table>
        <thead><tr><th>供应商</th><th>货号</th><th>三数量</th><th>命运</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in variances" :key="r.id">
            <td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td>
            <td>{{ r.expect_qty }} / {{ r.receive_qty }} / {{ r.confirm_qty }}</td>
            <td>{{ r.fate }}</td>
            <td>
              <template v-if="batch && batch.status === 'open'">
                <button class="secondary" @click="fate(r.id, 'accept_short')">认短</button>
                <button class="secondary" @click="fate(r.id, 'accept_over')">认溢</button>
                <button class="secondary" @click="fate(r.id, 'quarantine')">隔离</button>
              </template>
              <span v-else class="tag seal">只读</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
const day = ref('2026-11-18')
const batch = ref(null)
const matches = ref([])
const variances = ref([])
const err = ref('')
const ok = ref('')
const statusText = computed(() => ({ open: '未结案', sealed: '已结案', stale: '已失效' }[batch.value?.status] || batch.value?.status))
const statusClass = computed(() => ({ open: 'bad', sealed: 'seal', stale: 'void' }[batch.value?.status] || ''))
async function load() {
  err.value = ''
  const batches = await api(`/reconcile/batches?dock_day=${encodeURIComponent(day.value)}`)
  batch.value = batches[0] || null
  if (!batch.value) { matches.value = []; variances.value = []; return }
  matches.value = await api(`/reconcile/matches?batch_id=${batch.value.id}`)
  variances.value = await api(`/reconcile/variances?batch_id=${batch.value.id}`)
}
async function run() {
  err.value = ''; ok.value = ''
  try {
    batch.value = await api('/reconcile/run', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '对账完成'
    await load()
  } catch (e) { err.value = e.message }
}
async function seal() {
  err.value = ''; ok.value = ''
  try {
    batch.value = await api('/reconcile/seal', { method: 'POST', body: JSON.stringify({ dock_day: day.value }) })
    ok.value = '已结案'
    await load()
  } catch (e) { err.value = e.message }
}
async function fate(id, f) {
  err.value = ''; ok.value = ''
  try {
    await api(`/reconcile/variances/${id}/fate`, { method: 'POST', body: JSON.stringify({ fate: f }) })
    ok.value = '已更新命运'
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
