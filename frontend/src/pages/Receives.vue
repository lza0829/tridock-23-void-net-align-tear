<template>
  <div>
    <div class="hero"><div><h1>实收</h1><p>到货日实际点收行。结案后不可再写；作废为软作废，行保留可查。</p></div><button @click="load">刷新</button></div>
    <div class="panel">
      <div class="row">
        <input v-model="form.receive_no" placeholder="实收号" />
        <input v-model="form.supplier_code" placeholder="供应商编码" />
        <input v-model="form.sku" placeholder="货号" />
        <input v-model="form.dock_day" placeholder="到货日" />
        <input v-model.number="form.qty" type="number" placeholder="数量" />
        <button @click="add">登记实收</button>
      </div>
      <div class="row">
        <input v-model="operator" placeholder="操作人（作废必填）" />
        <label><input type="checkbox" v-model="showVoided" @change="load" /> 显示已作废</label>
      </div>
      <p class="msg">{{ err }}</p>
      <p class="okmsg">{{ ok }}</p>
      <table>
        <thead><tr><th>实收号</th><th>供应商</th><th>货号</th><th>到货日</th><th>数量</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id" :class="{ voided: r.voided }">
            <td>{{ r.receive_no }}</td><td>{{ r.supplier_code }}</td><td>{{ r.sku }}</td><td>{{ r.dock_day }}</td><td>{{ r.qty }}</td>
            <td><span v-if="r.voided" class="tag void" :title="`${r.voided_by} · ${r.void_reason || ''}`">已作废</span></td>
            <td><button v-if="!r.voided" class="secondary" @click="voidLine(r)">作废</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rows = ref([])
const err = ref('')
const ok = ref('')
const showVoided = ref(false)
const operator = ref(localStorage.getItem('operator') || '')
const form = ref({ receive_no: '', supplier_code: 'SUP-A', sku: '', dock_day: '2026-11-18', qty: 1 })
async function load() { rows.value = await api(`/receives${showVoided.value ? '?include_voided=1' : ''}`) }
async function add() {
  err.value = ''; ok.value = ''
  try {
    await api('/receives', { method: 'POST', body: JSON.stringify(form.value) })
    ok.value = '已登记'
    await load()
  } catch (e) { err.value = e.message }
}
async function voidLine(r) {
  err.value = ''; ok.value = ''
  localStorage.setItem('operator', operator.value)
  const reason = window.prompt(`作废实收单 ${r.receive_no} 的原因：`)
  if (reason === null) return
  try {
    await api(`/receives/${r.id}/void`, { method: 'POST', body: JSON.stringify({ operator: operator.value, reason }) })
    ok.value = `已作废 ${r.receive_no}`
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(load)
</script>
