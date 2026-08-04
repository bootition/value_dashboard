/**
 * 运行时形态探测（C3，报告41）：打包版（PyInstaller exe）与开发版入口不同，
 * 恢复指引需显示对应的 CLI 前缀（vd vs python -m app.cli.main）。
 */
import axios from 'axios'

let packaged: boolean | null = null

export async function isPackaged(): Promise<boolean> {
  if (packaged !== null) return packaged
  try {
    const resp = await axios.get<{ packaged?: boolean }>('/api/session')
    packaged = resp.data.packaged === true
  } catch {
    packaged = false
  }
  return packaged
}

export function resetRuntimeFlags(): void {
  packaged = null
}
