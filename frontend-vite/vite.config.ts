/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
  server: {
    port: 4000,
    proxy: {
      '/api': {
        target: process.env.BACKEND_URL ?? 'http://localhost:8002',
        changeOrigin: true,
        // SSE (text/event-stream) needs response streaming — disable compression
        configure: (proxy) => {
          proxy.on('proxyReq', (_proxyReq, req) => {
            if (req.headers.accept?.includes('text/event-stream')) {
              _proxyReq.setHeader('Accept-Encoding', 'identity')
            }
          })
        },
      },
    },
  },
})
