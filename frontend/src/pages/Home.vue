<template>
  <div>
    <div class="hero">
      <div>
        <h1>总览</h1>
        <p>当前到货日三腿行数（不含已作废）、对账批次与结案状态。结案后当日预报/实收/回执不可再改，也不可作废。</p>
      </div>
      <button @click="load">刷新</button>
    </div>
    <div class="grid" v-if="d">
      <div class="stat"><div class="k">到货日</div><div class="v">{{ d.dock_day }}</div></div>
      <div class="stat"><div class="k">预报 / 实收 / 回执</div><div class="v">{{ d.expect_count }} / {{ d.receive_count }} / {{ d.confirm_count }}</div></div>
      <div class="stat"><div class="k">已作废行</div><div class="v">{{ d.voided_count }}</div></div>
      <div class="stat"><div class="k">未决差额</div><div class="v">{{ d.pending_variance }}</div></div>
      <div class="stat">
        <div class="k">结案</div>
        <div class="v"><span class="tag" :class="d.sealed ? 'seal' : 'bad'">{{ d.sealed ? '已结案' : '未结案' }}</span></div>
      </div>
    </div>
    <div class="panel" v-if="d && d.latest_batch">
      <h2>最近对账批次</h2>
      <p>
        {{ d.latest_batch.batch_no }} · 对齐 {{ d.latest_batch.matched_count }} · 差额 {{ d.latest_batch.variance_count }} · 作废快照 {{ d.latest_batch.void_count }} ·
        <span class="tag" :class="batchClass">{{ batchText }}</span>
      </p>
    </div>
    <div class="panel">
      <h2>作废审计</h2>
      <table v-if="audits.length">
        <thead><tr><th>时间</th><th>腿别</th><th>单号</th><th>供应商</th><th>货号</th><th>数量</th><th>操作人</th><th>原因</th></tr></thead>
        <tbody>
          <tr v-for="a in audits" :key="a.id">
            <td>{{ a.created_at }}</td><td>{{ legText(a.leg) }}</td><td>{{ a.doc_no }}</td>
            <td>{{ a.supplier_code }}</td><td>{{ a.sku }}</td><td>{{ a.qty }}</td>
            <td>{{ a.operator }}</td><td>{{ a.reason || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else>暂无作废记录。</p>
    </div>
    <p class="msg">{{ err }}</p>
  </div>
</template>
<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
const d = ref(null)
const audits = ref([])
const err = ref('')
const batchText = computed(() => ({ open: '未结案', sealed: '已结案', stale: '已失效' }[d.value?.latest_batch?.status] || ''))
const batchClass = computed(() => ({ open: 'bad', sealed: 'seal', stale: 'void' }[d.value?.latest_batch?.status] || ''))
function legText(leg) {
  return { expect: '预报', receive: '实收', confirm: '回执' }[leg] || leg
}
async function load() {
  err.value = ''
  try {
    d.value = await api('/dashboard')
    audits.value = await api(`/void-audit?dock_day=${encodeURIComponent(d.value.dock_day)}`)
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
