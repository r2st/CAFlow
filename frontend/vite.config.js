import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Lets the dev server talk to the API without CORS or a baked-in host.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  // Test settings live in vitest.config.js, which takes precedence over this
  // file — keeping a second copy here would only drift out of step.
})
