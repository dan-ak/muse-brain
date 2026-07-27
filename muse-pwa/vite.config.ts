import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: true, // Expose to local network
    port: 3001, // Run web client on 3001 (leaving 3000 for the WebSocket server)
  }
})

