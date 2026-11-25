import { useEffect, useState } from 'react'
import { getConfig, parseRuntimeConfig } from '../../config'
import { useSessionStore } from '../../store'

export default function useHostFeatures() {
  const config = getConfig()
  const token = useSessionStore(s => s.adminToken)
  const [verified, setVerified] = useState<{ token: string; features: typeof config.features } | null>(null)
  useEffect(() => {
    if (!token || config.features.mode === 'local') return
    const controller = new AbortController()
    fetch(config.apiBase + '/config.json', { headers: { 'X-Admin-Token': token }, redirect: 'error',
      cache: 'no-store', signal: controller.signal })
      .then(async r => r.ok ? parseRuntimeConfig(await r.text()).features : null)
      .then(features => { if (!controller.signal.aborted && features) setVerified({ token, features }) })
      .catch(() => {})
    return () => controller.abort()
  }, [token, config])
  return token && verified?.token === token ? verified.features : config.features
}
