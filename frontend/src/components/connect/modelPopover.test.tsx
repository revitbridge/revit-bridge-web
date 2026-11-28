// @vitest-environment jsdom
import { act, useSyncExternalStore } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { getConfig, loadRuntimeConfig, subscribeConfig } from '../../config'
import { useSessionStore } from '../../store'
import ModelPopover from './ModelPopover'
import { endpoint } from './modelLibrary'

const entry = { name: 'Draft', source: 'saved', model: 'draft-model', vision: false }
const fallback = { model: 'original', base_url: 'https://original.example', key_set: true, key_tail: null }
const manage = vi.fn()
function Header() {
  const { features } = useSyncExternalStore(subscribeConfig, getConfig, getConfig)
  return <ModelPopover model={features.models.planner ?? ''} onManage={manage} />
}

async function mount(mode = 'local', library = [entry]) {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const calls = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/config.json') return Response.json({ features: { mode, models: { planner: 'original' }, modelLibrary: library } })
    if (url === endpoint + '/selection' && init?.method === 'PUT') {
      return Response.json({ configured: true, library, saved: {}, selection: { planner: { entry: entry.name } },
        models: { planner: { ...fallback, model: entry.model }, optimizer: { ...fallback, model: entry.model } } })
    }
    throw Error('Unexpected request')
  })
  vi.stubGlobal('fetch', calls)
  await loadRuntimeConfig()
  const node = document.createElement('div')
  document.body.append(node)
  const root = createRoot(node)
  await act(async () => root.render(<Header />))
  const button = node.querySelector('button')!
  const open = () => act(async () => button.click())
  return { node, button, open, calls, close: () => act(() => root.unmount()) }
}
afterEach(() => {
  vi.unstubAllGlobals()
  useSessionStore.setState(useSessionStore.getInitialState())
  document.body.replaceChildren()
  manage.mockClear()
})

it('lists the role defaults and entries, applies a selection and restores it after a failed write', async () => {
  const { node, button, calls, open, close } = await mount()
  expect(button.getAttribute('aria-haspopup')).toBe('dialog')
  expect(button.getAttribute('aria-expanded')).toBe('false')
  await open()
  expect(button.getAttribute('aria-expanded')).toBe('true')
  expect(node.querySelector('[role=dialog] h3')?.textContent).toBe('Models')
  expect([...node.querySelectorAll('label > span')].map(s => s.textContent)).toEqual(['Planner', 'Supervisor', 'Analyst', 'Optimizer'])
  const pick = node.querySelector('select')!
  expect([...pick.options].map(o => o.text)).toEqual(['Default (original)', 'Draft - draft-model'])
  expect(node.querySelectorAll('select')[1].options[0].text).toBe('Default (Not set)')
  async function choose(value: string) {
    await act(async () => { pick.value = value; pick.dispatchEvent(new Event('change', { bubbles: true })) })
  }
  await choose('Draft')
  const [url, init] = calls.mock.calls.at(-1)!
  expect(url).toBe(endpoint + '/selection')
  expect(init?.method).toBe('PUT')
  expect(JSON.parse(String(init?.body))).toEqual({ role: 'planner', entry: 'Draft' })
  expect(getConfig().features.modelSelection.planner).toBe('Draft')
  expect(button.textContent).toContain('draft-model')
  expect(node.textContent).toContain('This model cannot see sketches or captures.')
  expect(pick.options[0].text).toBe('Default (original)')
  calls.mockResolvedValueOnce(Response.json({ error: 'failure' }, { status: 409 }))
  await choose('')
  expect(JSON.parse(String(calls.mock.calls.at(-1)![1]?.body)).entry).toBeNull()
  expect(pick.value).toBe('Draft')
  expect(node.querySelector('[role=alert]')?.textContent).toBe('Could not change the model.')
  close()
})

it('closes on Escape, outside click and navigation to Settings', async () => {
  const { node, open, close } = await mount()
  await open()
  act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })))
  expect(node.querySelector('[role=dialog]')).toBeNull()
  await open()
  act(() => document.body.click())
  expect(node.querySelector('[role=dialog]')).toBeNull()
  await open()
  const link = node.querySelector('a')!
  expect(link.textContent).toBe('Manage models')
  expect(link.getAttribute('href')).toBe('/settings')
  act(() => link.click())
  expect(manage).toHaveBeenCalledOnce()
  expect(node.querySelector('[role=dialog]')).toBeNull()
  close()
})

it('disables cloud changes without a session token and explains an empty library', async () => {
  const cloud = await mount('cloud')
  await cloud.open()
  expect([...cloud.node.querySelectorAll('select')].every(s => s.disabled)).toBe(true)
  expect(cloud.node.textContent).toContain('An administrator chooses the models.')
  act(() => useSessionStore.setState({ adminToken: 'fixture-admin' }))
  expect(cloud.node.querySelector('select')?.disabled).toBe(false)
  cloud.close()
  const empty = await mount('local', [])
  await empty.open()
  expect(empty.node.textContent).toContain('The model library is empty.')
  expect(empty.node.querySelector('a')?.getAttribute('href')).toBe('/settings')
  empty.close()
})
