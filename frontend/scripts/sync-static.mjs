import { cp, mkdir, readdir, readFile, rm, stat } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { dirname, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const buildRoot = resolve(process.env.VD_FRONTEND_STATIC_SOURCE ?? resolve(frontendRoot, 'dist'))
const servedRoot = resolve(process.env.VD_FRONTEND_STATIC_DESTINATION ?? resolve(frontendRoot, '..', 'app', 'web', 'static'))
const stagingRoot = `${servedRoot}.staging-${process.pid}`

if (!(await stat(buildRoot)).isDirectory()) {
  throw new Error(`Frontend build output is missing: ${buildRoot}`)
}

async function manifest(root) {
  const files = []
  async function visit(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = resolve(directory, entry.name)
      if (entry.isDirectory()) await visit(path)
      else if (entry.isFile()) {
        const content = await readFile(path)
        files.push(`${relative(root, path).replaceAll('\\', '/')}:${content.length}:${createHash('sha256').update(content).digest('hex')}`)
      } else throw new Error(`Static bundle contains a non-file entry: ${path}`)
    }
  }
  await visit(root)
  return files.sort()
}

async function publishTree(source, target, relativePath = '') {
  await mkdir(target, { recursive: true })
  for (const entry of await readdir(source, { withFileTypes: true })) {
    const sourcePath = resolve(source, entry.name)
    const targetPath = resolve(target, entry.name)
    const nextRelative = relativePath ? `${relativePath}/${entry.name}` : entry.name
    if (entry.isDirectory()) await publishTree(sourcePath, targetPath, nextRelative)
    else if (entry.isFile() && nextRelative !== 'index.html') await cp(sourcePath, targetPath, { force: true })
    else if (!entry.isFile()) throw new Error(`Static bundle contains a non-file entry: ${sourcePath}`)
  }
}

async function removeStaleTree(root, sourceFiles, relativePath = '') {
  for (const entry of await readdir(root, { withFileTypes: true })) {
    const path = resolve(root, entry.name)
    const nextRelative = relativePath ? `${relativePath}/${entry.name}` : entry.name
    if (entry.isDirectory()) {
      await removeStaleTree(path, sourceFiles, nextRelative)
      if ((await readdir(path)).length === 0) await rm(path, { recursive: true, force: true })
    } else if (entry.isFile() && !sourceFiles.has(nextRelative)) {
      await rm(path, { force: true })
    }
  }
}

await rm(stagingRoot, { recursive: true, force: true })
await mkdir(stagingRoot, { recursive: true })
try {
  await cp(buildRoot, stagingRoot, { recursive: true })
  const [sourceManifest, stagedManifest] = await Promise.all([manifest(buildRoot), manifest(stagingRoot)])
  if (sourceManifest.join('\n') !== stagedManifest.join('\n')) {
    throw new Error('Static bundle copy verification failed')
  }

  // Keep the served tree continuously present. Copy assets first; only then
  // publish index.html, whose hashed references make the new bundle reachable.
  await publishTree(stagingRoot, servedRoot)
  await cp(resolve(stagingRoot, 'index.html'), resolve(servedRoot, 'index.html'), { force: true })

  const sourceFiles = new Map(sourceManifest.map(item => [item.split(':', 1)[0], item]))
  const servedManifest = await manifest(servedRoot)
  const servedFiles = new Map(servedManifest.map(item => [item.split(':', 1)[0], item]))
  for (const [path, digest] of sourceFiles) {
    if (servedFiles.get(path) !== digest) throw new Error('Served static bundle verification failed')
  }
  // Cleanup happens only after a complete new bundle is already live. A killed
  // cleanup can leave old unreferenced assets, but never removes the live UI.
  await removeStaleTree(servedRoot, sourceFiles)
  await rm(stagingRoot, { recursive: true, force: true })
} catch (error) {
  await rm(stagingRoot, { recursive: true, force: true })
  throw error
}
