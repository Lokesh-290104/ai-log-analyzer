import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  server: {
    // In development the React app runs on :5173 and the FastAPI backend on :8000.
    proxy: { '/api': 'http://localhost:8000' },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    css: false,
    // First (cold) runs compile everything; user-event typing tests can exceed the 5 s default.
    testTimeout: 15000,
  },
})
