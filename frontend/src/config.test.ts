import { describe, expect, it } from 'vitest'
import { parseRuntimeConfig } from './config'

describe('parseRuntimeConfig', () => {
  it('reads apiBase / wsBase / features and strips trailing slashes', () => {
    const cfg = parseRuntimeConfig(JSON.stringify({
      apiBase: 'https://api.example.com/',
      wsBase: 'wss://api.example.com/api/v1/bridge/ws/',
      features: { admin: true, maxDevices: 3 },
    }), 'application/json')
    expect(cfg.apiBase).toBe('https://api.example.com')
    expect(cfg.wsBase).toBe('wss://api.example.com/api/v1/bridge/ws')
    expect(cfg.features).toEqual({ mode: 'cloud', byoModel: true, serverModel: false, admin: true, maxDevices: 3 })
  })

  it('reads the deploy mode; a config that does not say local is a cloud host, as before modes', () => {
    const local = parseRuntimeConfig(JSON.stringify({ features: { mode: 'local', admin: false, maxDevices: 0 } }))
    expect(local.features).toEqual({ mode: 'local', byoModel: true, serverModel: false, admin: false, maxDevices: 0 })
    expect(parseRuntimeConfig(JSON.stringify({ features: { mode: 'cloud' } })).features.mode).toBe('cloud')
    expect(parseRuntimeConfig(JSON.stringify({ features: { mode: 'remote' } })).features.mode).toBe('cloud')
    expect(parseRuntimeConfig(JSON.stringify({ features: {} })).features.mode).toBe('cloud')
    expect(parseRuntimeConfig('{}').features.mode).toBe('cloud')
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
