import { rm } from 'node:fs/promises'
import { resolve } from 'node:path'

// Vite 8.1.5 在本仓库环境实测不清理 outDir 中的旧 hash 产物（即使
// emptyOutDir: true / --emptyOutDir）。prebuild 显式删除 dist，避免
// app/web/static 随 sync-static.mjs 持续累积历史构建垃圾。
const distRoot = resolve(process.cwd(), 'dist')
await rm(distRoot, { recursive: true, force: true })
