/**
 * Drift-proof unit test runner (2026-08-14 红队 P2-10).
 *
 * Runs every `tests/*.test.ts` file directly under `tests/` via
 * `node --experimental-strip-types --test`, so newly added root-level test
 * files are picked up automatically (previously the package.json script
 * hardcoded four file names and silently skipped new ones).
 * Component/E2E tests under `tests/component/` are run by vitest separately.
 */

import { readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { spawnSync } from 'node:child_process'

const testsDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'tests')
const files = readdirSync(testsDir)
  .filter((name) => name.endsWith('.test.ts'))
  .sort()
  .map((name) => join('tests', name))

if (files.length === 0) {
  console.error('[run-unit-tests] No tests/*.test.ts files found')
  process.exit(1)
}

const result = spawnSync(
  process.execPath,
  ['--experimental-strip-types', '--test', ...files],
  { stdio: 'inherit' },
)

process.exit(result.status ?? 1)
