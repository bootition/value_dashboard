<script setup lang="ts">
import { ref, h, computed } from 'vue'
import {
  NButton, NSpace, NInput, NDataTable, NModal, NForm, NFormItem,
  NTag, NEmpty, useMessage, useDialog, NCode, NDescriptions, NDescriptionsItem
} from 'naive-ui'
import axios from 'axios'

import type { DataTableColumns } from 'naive-ui'

interface DslExpression {
  id: number
  name: string
  version: number
  expression: string
  description: string
  status: 'draft' | 'validated' | 'single_previewed' | 'previewed' | 'published'
  created_at: string
}

interface DslValidateResult {
  valid: boolean
  expression: string
  expanded_expression: string
  historical_capable: boolean
  dependencies: string[]
  message: string
}

const message = useMessage()
const dialog = useDialog()
const showCreateModal = ref(false)
const showPreviewModal = ref(false)
const expanded = ref(false)
const expressions = ref<DslExpression[]>([])
const loading = ref(false)

const newExpression = ref({
  name: '',
  expression: '',
  description: '',
})

const previewResult = ref<DslValidateResult | Record<string, unknown> | null>(null)
const previewStockCode = ref('600519')

// L1-5（报告42）: 结构化预览的安全取值（校验类返回）
const previewMeta = computed(() => {
  const r = previewResult.value
  if (!r || !('valid' in r)) return null
  return r as DslValidateResult
})

// L1-5（报告42）: 状态中文化
const STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  validated: '已校验',
  single_previewed: '已单股预览',
  previewed: '已小样本预览',
  published: '已发布',
}

function statusTagType(s: string) {
  if (s === 'published') return 'success' as const
  if (s === 'validated') return 'info' as const
  if (s === 'single_previewed' || s === 'previewed') return 'warning' as const
  return 'default' as const
}

const columns: DataTableColumns<DslExpression> = [
  { title: '名称', key: 'name', width: 150 },
  { title: '表达式', key: 'expression', ellipsis: { tooltip: true } },
  { title: '描述', key: 'description', ellipsis: { tooltip: true } },
  {
    title: '状态',
    key: 'status',
    width: 110,
    render: (row: DslExpression) =>
      h(NTag, { type: statusTagType(row.status), size: 'small' }, () => STATUS_LABELS[row.status] || row.status),
  },
  { title: '创建时间', key: 'created_at', width: 160 },
  {
    title: '操作',
    key: 'actions',
    width: 360,
    render: (row: DslExpression) =>
      h(NSpace, {}, () => [
        h(NButton, {
          size: 'tiny',
          disabled: row.status !== 'draft',
          onClick: () => validateExpression(row),
        }, () => '校验'),
        h(NButton, {
          size: 'tiny',
          disabled: row.status !== 'validated',
          onClick: () => previewSingleExpression(row),
        }, () => '单股预览'),
        h(NButton, {
          size: 'tiny',
          disabled: row.status !== 'single_previewed',
          onClick: () => previewSampleExpression(row),
        }, () => '小样本预览'),
        h(NButton, {
          size: 'tiny',
          type: 'primary',
          disabled: row.status !== 'previewed',
          onClick: () => publishExpression(row),
        }, () => '发布'),
        h(NButton, { size: 'tiny', type: 'error', onClick: () => deleteExpression(row) }, () => '删除'),
      ]),
  },
]

async function loadExpressions() {
  loading.value = true
  try {
    const resp = await axios.get('/api/dsl/expressions')
    expressions.value = resp.data.expressions || []
  } catch {
    message.error('加载指标列表失败')
  } finally {
    loading.value = false
  }
}

async function createExpression() {
  if (!newExpression.value.name || !newExpression.value.expression) {
    message.warning('请填写名称和表达式')
    return
  }
  try {
    await axios.post('/api/dsl/expressions', newExpression.value)
    message.success('指标创建成功')
    showCreateModal.value = false
    newExpression.value = { name: '', expression: '', description: '' }
    loadExpressions()
  } catch (e: unknown) {
    const detail = axios.isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '创建失败'
    message.error(detail)
  }
}

async function validateExpression(expr: DslExpression) {
  try {
    const resp = await axios.post(`/api/dsl/expressions/${expr.name}/${expr.version}/validate`)
    previewResult.value = resp.data
    showPreviewModal.value = true
    loadExpressions()
  } catch (e: unknown) {
    const detail = axios.isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '校验失败'
    message.error(detail)
  }
}

async function previewSingleExpression(expr: DslExpression) {
  if (!previewStockCode.value.trim()) {
    message.warning('请输入单股预览代码')
    return
  }
  try {
    const resp = await axios.post(`/api/dsl/expressions/${expr.name}/${expr.version}/preview-single`, {
      stock_code: previewStockCode.value.trim(),
    })
    previewResult.value = resp.data
    showPreviewModal.value = true
    loadExpressions()
  } catch (e: unknown) {
    const detail = axios.isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '单股预览失败'
    message.error(detail)
  }
}

async function previewSampleExpression(expr: DslExpression) {
  try {
    const resp = await axios.post(`/api/dsl/expressions/${expr.name}/${expr.version}/preview-sample`)
    previewResult.value = resp.data
    showPreviewModal.value = true
    loadExpressions()
  } catch (e: unknown) {
    const detail = axios.isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '小样本预览失败'
    message.error(detail)
  }
}

async function publishExpression(expr: DslExpression) {
  try {
    await axios.put(`/api/dsl/expressions/${expr.id}/publish`)
    message.success('指标发布成功')
    loadExpressions()
  } catch (e: unknown) {
    const detail = axios.isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '发布失败'
    message.error(detail)
  }
}

async function deleteExpression(expr: DslExpression) {
  dialog.warning({
    title: '确认删除',
    content: `确定要删除指标 "${expr.name}" 吗？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await axios.delete(`/api/dsl/expressions/${expr.id}`)
        message.success('指标删除成功')
        loadExpressions()
      } catch (e: unknown) {
        const detail = axios.isAxiosError(e) ? e.response?.data?.detail : e instanceof Error ? e.message : '删除失败'
        message.error(detail)
      }
    },
  })
}

loadExpressions()
</script>

<template>
  <section class="dsl-workbench">
    <div class="dsl-heading"><div><p>COMPOSITE INDICATORS</p><h2>复合指标</h2><span>将已发布指标组合为可复用的研究条件。</span></div>
      <n-space>
        <n-button size="small" @click="expanded = !expanded">{{ expanded ? '收起' : '管理指标' }}</n-button>
        <n-button size="small" type="primary" @click="showCreateModal = true">创建指标</n-button>
      </n-space>
    </div>
    <p v-if="!expanded" class="dsl-summary">{{ expressions.length > 0 ? `已有 ${expressions.length} 个复合指标，可在筛选条件中直接使用。` : '暂无复合指标；创建后可作为筛选条件或排序字段使用。' }}</p>
    <template v-else>
      <div class="dsl-tools"><n-input v-model:value="previewStockCode" size="small" placeholder="单股预览代码" /><span>预览操作会使用这里的股票代码。</span></div>
      <n-empty v-if="expressions.length === 0 && !loading" description="暂无复合指标" class="dsl-empty" />
      <n-data-table
        v-else
        size="small"
        :columns="columns"
        :data="expressions"
        :loading="loading"
        :pagination="{ pageSize: 10 }"
      />
    </template>
    <n-modal v-model:show="showCreateModal" preset="dialog" title="创建复合指标" style="width: 600px;">
      <n-form>
        <n-form-item label="名称" required>
          <n-input v-model:value="newExpression.name" placeholder="例如: 价值得分" />
        </n-form-item>
        <n-form-item label="表达式" required>
          <n-input
            v-model:value="newExpression.expression"
            type="textarea"
            placeholder="例如: pe_ttm < 20 & roe > 0.15 & debt_ratio < 0.5"
            :rows="3"
          />
        </n-form-item>
        <n-form-item label="描述">
          <n-input v-model:value="newExpression.description" placeholder="指标用途说明" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">取消</n-button>
        <n-button type="primary" @click="createExpression">创建</n-button>
      </template>
    </n-modal>
    <n-modal v-model:show="showPreviewModal" preset="dialog" title="指标预览" style="width: 640px;">
      <template v-if="previewResult">
        <n-space vertical>
          <!-- L1-5（报告42）: 结构化预览：公式 / 值 / 置信度 / 依赖 / 失败原因 -->
          <n-descriptions v-if="previewMeta" :column="1" size="small" bordered>
            <n-descriptions-item label="校验结果">
              <n-tag :type="previewMeta.valid ? 'success' : 'error'" size="small">
                {{ previewMeta.valid ? '通过' : '失败' }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="公式">{{ previewMeta.expression || '—' }}</n-descriptions-item>
            <n-descriptions-item v-if="previewMeta.expanded_expression" label="展开公式">
              {{ previewMeta.expanded_expression }}
            </n-descriptions-item>
            <n-descriptions-item v-if="previewMeta.dependencies?.length" label="依赖指标">
              <n-tag v-for="dep in previewMeta.dependencies" :key="dep" size="small" style="margin-right: 4px;">{{ dep }}</n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="可用于历史序列">
              <n-tag :type="previewMeta.historical_capable ? 'success' : 'warning'" size="small">
                {{ previewMeta.historical_capable ? '是' : '否' }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item v-if="!previewMeta.valid && previewMeta.message" label="失败原因">
              <span style="color: #d03050;">{{ previewMeta.message }}</span>
            </n-descriptions-item>
          </n-descriptions>
          <!-- 原始返回（值明细等） -->
          <n-code :code="JSON.stringify(previewResult, null, 2)" language="json" style="max-height: 320px; overflow: auto;" />
        </n-space>
      </template>
      <template #action>
        <n-button @click="showPreviewModal = false">关闭</n-button>
      </template>
    </n-modal>
  </section>
</template>

<style scoped>
.dsl-workbench { padding: 25px; border-radius: 16px; background: #fff; box-shadow: 0 4px 17px rgba(48, 82, 59, .045); }.dsl-heading { display: flex; justify-content: space-between; align-items: start; gap: 16px; }.dsl-heading p { margin: 0; color: #91a097; font-size: 9px; font-weight: 800; letter-spacing: .13em; }.dsl-heading h2 { margin: 7px 0 5px; font-size: 18px; }.dsl-heading span, .dsl-summary, .dsl-tools span { color: #829087; font-size: 11px; }.dsl-summary { margin: 18px 0 0; }.dsl-tools { display: flex; align-items: center; gap: 10px; margin: 20px 0 12px; padding: 12px; border-radius: 8px; background: #fafcf9; }.dsl-tools :deep(.n-input) { width: 150px; }.dsl-empty { padding: 30px; }.dsl-workbench :deep(.n-data-table) { border: 1px solid #edf1ee; border-radius: 9px; }
</style>
