import { defineConfig, type ProxyOptions } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// No build-time API address: the SPA reads /config.json at startup.
// In development every backend path is proxied to a locally running host.
const devTarget = process.env.VITE_DEV_PROXY_TARGET || 'http://localhost:7860'
const DEV_PORT = 5173

// A local host takes changes (POST, PUT, PATCH, DELETE) only from its own origin
// (backend/localguard.py). This dev server's pages are that app too, so the proxy
// presents their Origin as the target's; any other Origin passes unchanged and is
// refused as before.
const DEV_ORIGINS = new Set([`http://localhost:${DEV_PORT}`, `http://127.0.0.1:${DEV_PORT}`, `http://[::1]:${DEV_PORT}`])
const asTargetOrigin: ProxyOptions['configure'] = proxy => {
  proxy.on('proxyReq', (proxyReq, req) => {
    if (DEV_ORIGINS.has(String(req.headers.origin))) proxyReq.setHeader('origin', new URL(devTarget).origin)
  })
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: DEV_PORT,
    proxy: {
      '/api': { target: devTarget, changeOrigin: true, ws: true, configure: asTargetOrigin },
      '/mcp': { target: devTarget, changeOrigin: true, configure: asTargetOrigin },
      '/health': { target: devTarget, changeOrigin: true },
      '/config.json': { target: devTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
  },
})
