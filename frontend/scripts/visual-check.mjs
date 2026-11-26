// From frontend: npm run build && node scripts/visual-check.mjs
// Uses an already installed Playwright. No installs, model server, or Revit session.
import { createServer } from 'node:http'
import { once } from 'node:events'
import { readFile, mkdir, readdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
import { join, dirname, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { fixtures } from './visual-fixtures.mjs'

const require = createRequire(import.meta.url)
const frontend = dirname(dirname(fileURLToPath(import.meta.url)))
const dist = join(frontend, 'dist')
const out = join(frontend, 'screenshots-check')

async function installedPlaywright() {
  const candidates = [process.env.PLAYWRIGHT_MODULE, 'playwright', 'playwright-core']
  // npx and Python Playwright both keep a full local Node driver.
  const cache = process.env.npm_config_cache || (process.platform === 'win32'
    ? join(process.env.LOCALAPPDATA, 'npm-cache') : join(process.env.HOME, '.npm'))
  for (const dir of await readdir(join(cache, '_npx')).catch(() => [])) {
    candidates.push(join(cache, '_npx', dir, 'node_modules', 'playwright'))
  }
  try {
    const path = execFileSync('python', ['-c', "import pathlib, playwright; print(pathlib.Path(playwright.__file__).parent / 'driver' / 'package')"],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim()
    candidates.push(path)
  } catch { /* Python Playwright is optional. */ }
  let installed
  for (const id of candidates.filter(Boolean)) {
    try {
      const driver = require(id)
      installed ??= driver
      if (existsSync(driver.chromium.executablePath())) return driver
    } catch { /* Try the next local installation. */ }
  }
  return installed
}

async function walk(browser, origin, language, width, height, scheme) {
  const zh = language === 'zh'
  const ctx = await browser.newContext({ viewport: { width, height }, locale: zh ? 'zh-CN' : 'en-US',
    colorScheme: scheme, reducedMotion: 'reduce', serviceWorkers: 'block' })
  const errors = []
  try {
    const page = await ctx.newPage()
    page.setDefaultTimeout(6000)
    page.on('pageerror', error => errors.push(error.message))
    // A diagram of a fictional view, used solely as the mock view_image payload.
    await page.setContent('<svg width="320" height="180" style="display:block" viewBox="0 0 320 180"><rect width="320" height="180" fill="#f0f3ed"/><g fill="none" stroke="#465955" stroke-width="4"><path d="M28 20h266v140H28z M128 20v54m0 22v64M28 96h55m24 0h106m26 0h55M215 20v76"/></g><g stroke="#82aa9e" fill="none"><path d="M128 74a22 22 0 0 0 22 22h-22 M83 96a24 24 0 0 1 24-24v24"/><path d="M40 32h48v30H40zM166 36h34v38h-34zM244 112h34v34h-34z"/></g></svg>')
    const png = (await page.locator('svg').screenshot()).toString('base64')
    const mock = fixtures(zh, png)
    await ctx.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== origin) {
        errors.push('Unexpected origin ' + url.origin)
        return route.abort()
      }
      if (url.pathname === '/api/chat') return route.fulfill({ contentType: 'text/event-stream',
        headers: { 'X-Session-Id': 'studio08-demo' }, body: mock.chat })
      if (Object.hasOwn(mock.routes, url.pathname)) return route.fulfill({ json: mock.routes[url.pathname] })
      if (url.pathname.startsWith('/api/')) {
        errors.push('Missing fixture ' + url.pathname)
        return route.fulfill({ status: 404, json: { error: 'no_fixture' } })
      }
      return route.continue()
    })
    await ctx.addInitScript(lang => localStorage.setItem('rb_language', lang), language)
    const folder = join(out, language + '-' + width + 'x' + height + '-' + scheme)
    await mkdir(folder, { recursive: true })
    async function shot(name) {
      await page.screenshot({ path: join(folder, name + '.png'), animations: 'disabled' })
      const overflow = await page.locator('html, main').evaluateAll(nodes => nodes.some(n => n.scrollWidth > n.clientWidth + 1))
      if (overflow) errors.push(name + ': horizontal page overflow')
    }
    await page.goto(origin)
    await page.locator('.current-selection button').waitFor()
    await shot('chat-empty')
    await page.locator('.context-trigger').click()
    if (width < 1040) {
      await page.locator('#project-context[open]').waitFor()
      await shot('chat-context')
      await page.keyboard.press('Escape')
    } else await page.locator('.context-trigger').click()
    await page.locator('.mention-textarea').fill(zh ? '检查所选隔墙，墙高为 3200 mm。' : 'Review the selected partitions at a height of 3200 mm.')
    await page.locator('.composer-actions .btn-primary').click()
    await page.locator('.spec-card').waitFor()
    await page.locator('.current-selection button').waitFor()
    await shot('chat-conversation')
    // A tall transcript needs multiple viewport shots, especially at phone width.
    for (const [name, selector] of Object.entries({ capture: '.chat-capture', plan: '.task-plan', card: '.spec-card', question: '.inline-form' })) {
      await page.locator(selector).evaluate(el => el.scrollIntoView({ block: 'center' }))
      await shot('chat-' + name)
    }
    await page.locator('.composer-actions .btn-secondary').click()
    await page.locator('.tool-drawer .catalog-row').first().waitFor()
    await shot('tool-drawer')
    await page.locator('.catalog-row').filter({ hasText: 'create_wall' }).click()
    await page.locator('.catalog-detail table').scrollIntoViewIfNeeded()
    await shot('tool-details')
    await page.keyboard.press('Escape')

    const views = [
      ['tools', '/tools', '.pack-table tbody tr'], ['workbench', '/capabilities/workbench', '.hostv2-workbench-editor'],
      ['authoring', '/capabilities/authoring', '.authoring-page li'], ['workflows', '/workflows', '.workflow-packs li'],
      ['evidence', '/evidence', '.evidence-row'], ['settings-models', '/settings', 'form input[type=url]'],
      ['settings-connection', '/settings/connection', '.bridge-status-row'], ['settings-remote', '/settings/remote-mcp', '.remote-mcp table'],
      ['settings-usage', '/token', '.token-run'],
    ]
    for (const [name, path, ready] of views) {
      await page.goto(origin + path)
      await page.locator(ready).first().waitFor()
      if (name === 'workflows') {
        await page.locator('.workflow-actions select').selectOption('level_wall')
        await page.locator('.workflow-steps ol > li').nth(1).waitFor()
      }
      await shot(name)
      if (name === 'workflows') {
        await page.locator('.workflow-steps ol > li').last().scrollIntoViewIfNeeded()
        await shot('workflow-steps')
      }
      if (name === 'evidence') {
        await page.locator('.record-id').first().click()
        await page.locator('.evidence-inspector').scrollIntoViewIfNeeded()
        await shot('evidence-detail')
      }
    }
    if (errors.length) throw new Error([...new Set(errors)].join('\n'))
    console.log('Captured ' + folder)
  } finally { await ctx.close() }
}

async function main() {
  if (!existsSync(join(dist, 'index.html'))) throw new Error('Build frontend first with npm run build.')
  const pw = await installedPlaywright()
  if (!pw) { console.log('SKIP: Playwright is unavailable.'); return }
  let browser
  try { browser = await pw.chromium.launch({ headless: true, args: ['--disable-background-networking', '--no-proxy-server'] }) }
  catch (err) { console.log('SKIP: Chromium launch failed. ' + err.message.split('\n')[0]); return }
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, 'http://127.0.0.1').pathname
    // Serve only the compiled UI and its icons. Mock API requests are intercepted above.
    const asset = /^\/assets\/[\w.-]+$|^\/(?:favicon[\w.-]*|apple-touch-icon\.png)$/.test(path)
    const file = asset ? path.slice(1) : 'index.html'
    try {
      const body = await readFile(join(dist, file))
      const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' }
      res.writeHead(200, { 'Content-Type': mime[extname(file)] }).end(body)
    } catch { res.writeHead(404).end() }
  })
  try {
    server.listen(0, '127.0.0.1')
    await once(server, 'listening')
    const origin = 'http://127.0.0.1:' + server.address().port
    for (const language of ['en','zh']) for (const [width,height] of [[1440,900],[390,844]]) {
      for (const scheme of ['light','dark']) await walk(browser, origin, language, width, height, scheme)
    }
  } finally { server.close(); await browser.close() }
}
await main()
