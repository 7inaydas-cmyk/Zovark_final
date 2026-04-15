import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Audit 4.11: production builds must not ship sourcemaps. An operator who
  // toggles `sourcemap: true` during local profiling would otherwise publish
  // minified JS + the unminified source alongside it.
  build: {
    sourcemap: false,
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8090',
        changeOrigin: true,
      },
      // Browser OTLP → local SigNoz collector (Ticket 10). Strip /otel so /otel/v1/traces → /v1/traces on :4318.
      '/otel': {
        target: 'http://127.0.0.1:4318',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/otel/, ''),
      },
    }
  }
})
