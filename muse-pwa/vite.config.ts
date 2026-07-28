import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The app derives its websocket URL from window.location, so in production the
// bundle and the socket share one origin. Proxying here keeps that true during
// development, where Vite serves the page instead of server.py.
//
// Not read from process.env: this config is type-checked by `tsc -b` without
// @types/node, and one editable constant is not worth that dependency.
const SERVER_ORIGIN = 'http://localhost:8443'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // Expose to local network
    port: 3001,
    proxy: {
      '/ws': { target: SERVER_ORIGIN, ws: true },
      '/api': { target: SERVER_ORIGIN },
      '/healthz': { target: SERVER_ORIGIN },
    },
  },
})
