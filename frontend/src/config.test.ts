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
    expect(cfg.features).toEqual({ byoModel: true, serverModel: false, admin: true, maxDevices: 3 })
  })

  it('accepts the static config.example.json shape a split deployment ships', async () => {
    const example = await import('../public/config.example.json')
    const cfg = parseRuntimeConfig(JSON.stringify(example.default))
    expect(cfg.apiBase).toBe('https://api.example.com')
    expect(cfg.features.maxDevices).toBe(20)
  })

  it('refuses the SPA fallback page a static host returns instead of the file', () => {
    expect(() => parseRuntimeConfig('<!doctype html><html></html>', 'text/html')).toThrow(/not JSON/)
    expect(() => parseRuntimeConfig('{"apiBase": ""}', 'text/html; charset=utf-8')).toThrow(/not JSON/)
  })
})
