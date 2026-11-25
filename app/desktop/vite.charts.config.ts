import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Dev-only build for the chart-primitives visual harness. It is a separate config so the harness
// (and its synthetic fixtures) never enters the application bundle produced by vite.config.ts.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: './',
  build: {
    outDir: 'dist-charts',
    emptyOutDir: true,
    target: 'chrome120',
    rollupOptions: {
      input: {
        charts: fileURLToPath(new URL('./charts-preview.html', import.meta.url)),
        analytics: fileURLToPath(new URL('./analytics-preview.html', import.meta.url)),
      },
    },
  },
});
