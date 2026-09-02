import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend is proxied under /api so the frontend needs no CORS config and no
// environment variable to find it in development.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
