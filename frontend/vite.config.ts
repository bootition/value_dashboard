import { defineConfig } from 'vitest/config'
import { resolve } from 'node:path'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  build: {
    // Build within the frontend root, then the build script clean-copies this
    // exact tree into FastAPI/PyInstaller's served static directory.
    outDir: resolve(__dirname, 'dist'),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
  test: {
    // 组件/浏览器流程级测试（发布级红队 P1: 前端 E2E 缺口）
    environment: 'jsdom',
    include: ['tests/component/**/*.test.ts'],
    setupFiles: ['tests/component/setup.ts'],
    globals: false,
  },
})
