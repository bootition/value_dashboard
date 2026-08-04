import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import { installWriteTokenInterceptor } from './http'
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
      path: '/stock',
      name: 'stock-search',
      component: () => import('./views/StockSearchPage.vue'),
      meta: { title: '个股详情' },
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
      path: '/design-preview',
      name: 'design-preview',
      component: () => import('./views/ResearchWorkbenchPreviewPage.vue'),
      meta: { title: '筛选工作台样稿', staticPreview: true },
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
installWriteTokenInterceptor()
app.use(router)
app.mount('#app')
