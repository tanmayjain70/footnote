import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // The API is proxied rather than called cross-origin in development, so the
    // dev setup matches production (same-origin behind one domain) and CORS
    // problems surface in staging rather than only after deploy.
    proxy: {
      '/api': {
        // Overridable because 8000 is a popular port and you will not always
        // have it: `set VITE_PROXY_TARGET=http://127.0.0.1:8010 && npm run dev`.
        target: process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
