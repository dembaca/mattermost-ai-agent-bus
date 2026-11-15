import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Served from https://dembaca.github.io/mattermost-ai-agent-bus/ (GitHub Pages).
export default defineConfig({
  base: '/mattermost-ai-agent-bus/',
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
})
