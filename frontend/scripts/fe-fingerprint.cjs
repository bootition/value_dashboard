// Start.bat frontend freshness helper (node, no deps).
//
// The fingerprint is the sha256 of every file that defines the frontend build:
// `src/**`, the app HTML entry, toolchain config and the lockfile. It does NOT
// include build output (`dist/`, `app/web/static/`) so that committed
// generated bundles stay deterministic and committed `index.html` churn does
// not force a rebuild.
//
// Modes:
//   fe-fingerprint.cjs                  -> print the current source fingerprint
//   fe-fingerprint.cjs --stamp <path>   -> write the current fingerprint to <path>
//   fe-fingerprint.cjs --check <path>   -> exit 0 when <path> matches the source
//                                          fingerprint AND the served bundle is
//                                          present and complete; exit 1 otherwise
'use strict'

const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')

const frontendRoot = path.resolve(__dirname, '..')
const appRoot = path.resolve(frontendRoot, '..')

const TOP_LEVEL_SOURCES = [
  'index.html',
  'package.json',
  'package-lock.json',
  'vite.config.ts',
  'tsconfig.json',
  'tsconfig.app.json',
  'tsconfig.node.json',
  'eslint.config.js',
]

function collectFiles(dir, root, acc) {
  let entries
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true })
  } catch {
    return
  }
  for (const entry of entries) {
    const absolute = path.join(dir, entry.name)
    const relative = path.relative(root, absolute)
    if (entry.isDirectory()) {
      if (relative.startsWith('dist') || relative.startsWith('node_modules') || relative.startsWith('test-results')) continue
      collectFiles(absolute, root, acc)
    } else if (entry.isFile() && !relative.startsWith('dist/') && !relative.startsWith('node_modules/')) {
      acc.push(relative.replaceAll('\\', '/'))
    }
  }
}

function sourceFiles() {
  const files = [...TOP_LEVEL_SOURCES]
  if (fs.existsSync(path.join(frontendRoot, 'src'))) {
    collectFiles(path.join(frontendRoot, 'src'), frontendRoot, files)
  }
  return files.sort()
}

function fingerprint() {
  const hash = crypto.createHash('sha256')
  for (const relative of sourceFiles()) {
    const absolute = path.join(frontendRoot, relative)
    if (!fs.existsSync(absolute)) continue
    const stat = fs.statSync(absolute)
    if (!stat.isFile()) continue
    hash.update(relative)
    hash.update('\0')
    hash.update(fs.readFileSync(absolute))
    hash.update('\0')
  }
  return hash.digest('hex')
}

function servedBundleIsComplete() {
  const entry = path.join(appRoot, 'app', 'web', 'static', 'index.html')
  let html
  try {
    html = fs.readFileSync(entry, 'utf8')
  } catch {
    return false
  }
  const references = /(?:src|href)="(\/assets\/[^"]+)"/g
  let matched = 0
  let match
  while ((match = references.exec(html)) !== null) {
    matched += 1
    const candidate = path.join(appRoot, 'app', 'web', 'static', match[1].replace(/^\//, ''))
    if (!fs.existsSync(candidate)) return false
  }
  return matched > 0
}

function main() {
  const args = process.argv.slice(2)
  const current = fingerprint()

  if (args[0] === '--stamp') {
    const stamp = args[1]
    if (!stamp) process.exitCode = 2
    try {
      fs.writeFileSync(stamp, `${current}\n`)
    } catch {
      process.exitCode = 2
    }
    return
  }

  if (args[0] === '--check') {
    const stamp = args[1]
    if (!stamp || !fs.existsSync(stamp)) process.exitCode = 1
    const created = fs.readFileSync(stamp, 'utf8').trim().split('\n').pop()
    if (created !== current) process.exitCode = 1
    if (process.exitCode === 0 && !servedBundleIsComplete()) process.exitCode = 1
    return
  }

  process.stdout.write(`${current}\n`)
}

main()