import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'path'
import { resolveAllowedHosts } from './dev-hosts'

// Hosts aceitos pelo dev server: SEMPRE um ARRAY (nunca `true`) = localhost/127.0.0.1 + o host de
// cada origem de RD_DEV_ALLOWED_ORIGINS (ops/dev/dev-origins.env — ex.: túnel ngrok; ver
// ops/README.md §3.1). Entrada inválida (curinga, caminho, sem esquema…) → o Vite não sobe.
// Nomes internos (frontend/rd-frontend) não são necessários: o nginx repassa o Host do cliente
// e o healthcheck usa 127.0.0.1.
const allowedHosts = resolveAllowedHosts({
  base: ['localhost', '127.0.0.1'],
  origins: process.env.RD_DEV_ALLOWED_ORIGINS,
  originsSource: 'RD_DEV_ALLOWED_ORIGINS',
})

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    allowedHosts,
    // HMR sem `server.hmr`: o cliente usa esquema/host/porta da PÁGINA, sempre via nginx —
    // http://localhost → ws://localhost/ ; https://<túnel> → wss://<túnel>/ (sem mixed content).
    // Observação de ARQUIVOS do HMR no volume Docker (não é polling da aplicação).
    watch: {
      usePolling: true,
      interval: 1000,
    },
    proxy: {
      '/api': {
        target: 'http://backend:8000',
        changeOrigin: true,
        secure: false,
      },
      '/ws': {
        target: 'ws://backend:8000',
        ws: true,
        changeOrigin: true,
        secure: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (/[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id)) return 'react'
          if (id.includes('react-router')) return 'router'
          if (id.includes('@tanstack')) return 'query'
          if (id.includes('@radix-ui')) return 'radix'
          if (/recharts|d3-|victory-vendor/.test(id)) return 'charts'
          if (/react-hook-form|zod|@hookform/.test(id)) return 'forms'
          return undefined
        },
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
  },
})
