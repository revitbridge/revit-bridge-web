import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// No build-time API address: the SPA reads /config.json at startup.
// In development every backend path is proxied to a locally running host.
const devTarget = process.env.VITE_DEV_PROXY_TARGET || 'http://localhost:7860'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: devTarget, changeOrigin: true, ws: true },
      '/health': { target: devTarget, changeOrigin: true },
      '/config.json': { target: devTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
  },
})
