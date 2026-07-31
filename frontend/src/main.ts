import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import axios from 'axios'
import App from './App.vue'
import './style.css'

// 路由定义 - 4个页面 (PRD §5)
const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/screening',
    },
    {
      path: '/screening',
      name: 'screening',
      component: () => import('./views/ScreeningPage.vue'),
      meta: { title: '筛选' },
    },
    {
      path: '/watchlist',
      name: 'watchlist',
      component: () => import('./views/WatchlistPage.vue'),
      meta: { title: '自选列表' },
    },
    {
      path: '/stock/:code',
      name: 'stock-detail',
      component: () => import('./views/StockDetailPage.vue'),
      meta: { title: '个股详情' },
    },
    {
      path: '/data-status',
      name: 'data-status',
      component: () => import('./views/DataStatusPage.vue'),
      meta: { title: '数据状态' },
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('./views/NotFoundPage.vue'),
      meta: { title: '页面不存在' },
    },
  ],
})

// 路由导航后更新页面标题
router.afterEach((to) => {
  const title = to.meta.title as string
  document.title = title ? `${title} - Value Dashboard` : 'Value Dashboard'
})

const app = createApp(App)
let writeToken: string | null = null
axios.interceptors.request.use(async (request) => {
  if (!['post', 'put', 'patch', 'delete'].includes(request.method?.toLowerCase() ?? '')) return request
  if (writeToken === null) {
    const response = await axios.get<{ write_token: string }>('/api/session')
    writeToken = response.data.write_token
  }
  request.headers.set('X-VD-Write-Token', writeToken)
  return request
})
app.use(router)
app.mount('#app')
