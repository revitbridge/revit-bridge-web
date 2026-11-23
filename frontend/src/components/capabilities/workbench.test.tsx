/* The workbench page placeholder, its address, and the link the chat model writes to it. */

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import App from '../../App'
import type { ImportResult } from '../../api/workbench'
import { pathOf, viewOf, WORKBENCH_PATH } from '../../route'
import Markdown from '../shared/Markdown'
import { BridgeApiError } from '../shared/http'
import CapabilitiesPage from '../pages/CapabilitiesPage'
import { importPack, ownTools } from './ownTools'
import WorkbenchPlaceholder from './WorkbenchPlaceholder'

const imported: ImportResult = { status: 'imported', name: 'mine', version: '1.0.0', replaced: false, revit_synced: false }

describe('the workbench address', () => {
  it('opens the Capabilities page at its workbench; every other path opens the first tab', () => {
    expect(WORKBENCH_PATH).toBe('/capabilities/workbench')
    for (const path of ['/capabilities/workbench', '/capabilities/workbench/', '/Capabilities/Workbench']) {
      expect(viewOf(path)).toEqual({ tab: 2, capabilities: 'workbench' })
    }
    for (const path of ['/', '/task', '/capabilities', '/capabilities/packs', '/capabilities/workbench/x']) {
      expect(viewOf(path)).toEqual({ tab: 0, capabilities: 'packs' })
    }
    expect(pathOf({ tab: 2, capabilities: 'workbench' })).toBe(WORKBENCH_PATH)
    expect(pathOf({ tab: 2, capabilities: 'packs' })).toBe('/')
    expect(pathOf({ tab: 1, capabilities: 'workbench' })).toBe('/')
  })

  it('renders the placeholder there', () => {
    const html = renderToStaticMarkup(<App path="/capabilities/workbench" />)
    expect(html).toContain('<button class="tab-button is-active">Capabilities</button>')
    expect(html).toContain('<button class="subtab is-active">Workbench</button>')
    expect(html).toContain('The workbench UI is being wired in')
    expect(renderToStaticMarkup(<App path="/" />)).not.toContain('The workbench UI is being wired in')
  })

  it('the chat renders the link the host paragraph asks for, opening in a new tab', () => {
    const html = renderToStaticMarkup(<Markdown>{'No tool fits. Make one in the [workbench](/capabilities/workbench).'}</Markdown>)
    expect(html).toContain('<a href="/capabilities/workbench" target="_blank" rel="noopener noreferrer">workbench</a>')
  })
})

describe('the placeholder', () => {
  it('shows the note, the import control and the subtabs', () => {
    const html = renderToStaticMarkup(<WorkbenchPlaceholder api={{ list: async () => [], remove: async () => {},
      exportYaml: async () => '', importYaml: async () => imported }} />)
    expect(html).toContain('The workbench UI is being wired in')
    expect(html).toContain('Loading your tools...')
    expect(renderToStaticMarkup(<CapabilitiesPage />)).toContain('<button class="subtab">Workbench</button>')
  })

  it('lists only the designer own tools', () => {
    expect(ownTools([
      { name: 'create_wall', description: '', builtin: true, origin: 'builtin' },
      { name: 'mine', description: '', builtin: false, origin: 'user', directory: 'D' },
    ]).map(t => t.name)).toEqual(['mine'])
  })

  it('imports, and asks before replacing a tool of the same name', async () => {
    const exists = new BridgeApiError(409, JSON.stringify({ error: 'pack_exists', message: 'exists', tool: 'mine' }))
    const importYaml = vi.fn(async (_text: string, options?: { overwrite?: boolean }) => {
      if (!options?.overwrite) throw exists
      return { ...imported, replaced: true }
    })
    const asked: string[] = []
    expect(await importPack({ importYaml }, 'yaml', name => { asked.push(name); return false })).toBeNull()
    expect(asked).toEqual(['mine'])
    expect(importYaml).toHaveBeenCalledTimes(1)
    expect(await importPack({ importYaml }, 'yaml', () => true)).toEqual({ ...imported, replaced: true })
    expect(importYaml).toHaveBeenLastCalledWith('yaml', { overwrite: true })

    const builtin = new BridgeApiError(409, JSON.stringify({ error: 'builtin_read_only', message: 'read-only', tool: 'create_wall' }))
    await expect(importPack({ importYaml: async () => { throw builtin } }, 'yaml', () => true)).rejects.toBe(builtin)
    expect(await importPack({ importYaml: async () => imported }, 'yaml', () => false)).toEqual(imported)
  })
})
