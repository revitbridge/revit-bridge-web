import { describe, expect, it } from 'vitest'
import { parseRuntimeConfig } from './config'

const NO_MODELS = {
  models: { analyst: null, planner: null, supervisor: null }, modelsFile: false, roles: false,
  chatModes: [], chatModeDefault: null, runModes: ['confirm'],
}

describe('parseRuntimeConfig', () => {
  it('reads apiBase / wsBase / features and strips trailing slashes', () => {
    const cfg = parseRuntimeConfig(JSON.stringify({
      apiBase: 'https://api.example.com/',
      wsBase: 'wss://api.example.com/api/v1/bridge/ws/',
      features: { admin: true, maxDevices: 3 },
    }), 'application/json')
    expect(cfg.apiBase).toBe('https://api.example.com')
    expect(cfg.wsBase).toBe('wss://api.example.com/api/v1/bridge/ws')
    expect(cfg.features).toEqual({ mode: 'cloud', byoModel: true, serverModel: false, admin: true, maxDevices: 3, ...NO_MODELS })
  })

  it('reads the deploy mode; a config that does not say local is a cloud host, as before modes', () => {
    const local = parseRuntimeConfig(JSON.stringify({ features: { mode: 'local', admin: false, maxDevices: 0 } }))
    expect(local.features).toEqual({ mode: 'local', byoModel: true, serverModel: false, admin: false, maxDevices: 0, ...NO_MODELS })
    expect(parseRuntimeConfig(JSON.stringify({ features: { mode: 'cloud' } })).features.mode).toBe('cloud')
    expect(parseRuntimeConfig(JSON.stringify({ features: { mode: 'remote' } })).features.mode).toBe('cloud')
    expect(parseRuntimeConfig(JSON.stringify({ features: {} })).features.mode).toBe('cloud')
    expect(parseRuntimeConfig('{}').features.mode).toBe('cloud')
  })

  it('reads the model name of each role and where the names come from; anything else is no name', () => {
    const file = parseRuntimeConfig(JSON.stringify({ features: {
      models: { analyst: 'small-model', planner: 'main-model', supervisor: 'small-model' }, modelsFile: true } }))
    expect(file.features.models).toEqual({ analyst: 'small-model', planner: 'main-model', supervisor: 'small-model' })
    expect(file.features.modelsFile).toBe(true)
    const odd = parseRuntimeConfig(JSON.stringify({ features: {
      models: { analyst: 7, planner: '', supervisor: null, reviewer: 'x' }, modelsFile: 'yes' } }))
    expect(odd.features.models).toEqual({ analyst: null, planner: null, supervisor: null })
    expect(odd.features.modelsFile).toBe(false)
    expect(parseRuntimeConfig(JSON.stringify({ features: { models: 'main' } })).features.models)
      .toEqual({ analyst: null, planner: null, supervisor: null })
    // An older host without the fields: no names, not from a file, no roles.
    expect(parseRuntimeConfig('{}').features).toMatchObject(NO_MODELS)
  })

  it('reads whether the chat runs in roles: only true is true (an older host ran one loop)', () => {
    expect(parseRuntimeConfig(JSON.stringify({ features: { roles: true } })).features.roles).toBe(true)
    expect(parseRuntimeConfig(JSON.stringify({ features: { roles: false } })).features.roles).toBe(false)
    expect(parseRuntimeConfig(JSON.stringify({ features: { roles: 'yes' } })).features.roles).toBe(false)
    expect(parseRuntimeConfig('{}').features.roles).toBe(false)
  })

  it('reads the chat modes the host offers and what auto runs; anything else is none', () => {
    const local = parseRuntimeConfig(JSON.stringify({ features: {
      chatModes: ['auto', 'single', 'roles', 'roles_analyst'], chatModeDefault: 'roles' } }))
    expect(local.features.chatModes).toEqual(['auto', 'single', 'roles', 'roles_analyst'])
    expect(local.features.chatModeDefault).toBe('roles')
    const odd = parseRuntimeConfig(JSON.stringify({ features: {
      chatModes: ['auto', 'fast', 7, 'roles', 'roles'], chatModeDefault: 'auto' } }))
    expect(odd.features.chatModes).toEqual(['auto', 'roles'])
    expect(odd.features.chatModeDefault).toBeNull()                    // auto is never what auto runs
    expect(parseRuntimeConfig(JSON.stringify({ features: { chatModes: 'roles' } })).features.chatModes).toEqual([])
    expect(parseRuntimeConfig('{}').features).toMatchObject({ chatModes: [], chatModeDefault: null })
  })

  it('accepts the static config.example.json shape a split deployment ships', async () => {
    const example = await import('../public/config.example.json')
    const cfg = parseRuntimeConfig(JSON.stringify(example.default))
    expect(cfg.apiBase).toBe('https://api.example.com')
    expect(cfg.features.maxDevices).toBe(20)
    expect(cfg.features.mode).toBe('cloud')          // a split deployment is a cloud host
  })

  it('refuses the SPA fallback page a static host returns instead of the file', () => {
    expect(() => parseRuntimeConfig('<!doctype html><html></html>', 'text/html')).toThrow(/not JSON/)
    expect(() => parseRuntimeConfig('{"apiBase": ""}', 'text/html; charset=utf-8')).toThrow(/not JSON/)
  })
})
