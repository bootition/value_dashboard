<script setup lang="ts">
import { ref, h } from 'vue'
import {
  NCard, NButton, NSpace, NInput, NDataTable, NModal, NForm, NFormItem,
  NTag, NEmpty, useMessage, useDialog, NCode
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
const expressions = ref<DslExpression[]>([])
const loading = ref(false)

const newExpression = ref({
  name: '',
  expression: '',
  description: '',
})

const previewResult = ref<DslValidateResult | Record<string, unknown> | null>(null)
const previewStockCode = ref('600519')

function statusTagType(s: string) {
  if (s === 'published') return 'success' as const
  if (s === 'validated') return 'info' as const
  return 'default' as const
}

const columns: DataTableColumns<DslExpression> = [
  { title: '名称', key: 'name', width: 150 },
  { title: '表达式', key: 'expression', ellipsis: { tooltip: true } },
  { title: '描述', key: 'description', ellipsis: { tooltip: true } },
  {
    title: '状态',
    key: 'status',
    width: 100,
    render: (row: DslExpression) =>
      h(NTag, { type: statusTagType(row.status), size: 'small' }, () => row.status),
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
  <n-card title="复合指标管理" size="small">
    <template #header-extra>
      <n-space>
        <n-input v-model:value="previewStockCode" size="small" placeholder="单股代码" style="width: 110px" />
        <n-button size="small" type="primary" @click="showCreateModal = true">创建指标</n-button>
      </n-space>
    </template>
    <n-empty v-if="expressions.length === 0 && !loading" description="暂无复合指标" />
    <n-data-table
      v-else
      size="small"
      :columns="columns"
      :data="expressions"
      :loading="loading"
      :pagination="{ pageSize: 10 }"
    />
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
    <n-modal v-model:show="showPreviewModal" preset="dialog" title="指标预览" style="width: 600px;">
      <n-space vertical>
        <n-code :code="JSON.stringify(previewResult, null, 2)" language="json" />
      </n-space>
      <template #action>
        <n-button @click="showPreviewModal = false">关闭</n-button>
      </template>
    </n-modal>
  </n-card>
</template>
