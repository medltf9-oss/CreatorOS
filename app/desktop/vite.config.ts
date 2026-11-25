import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: './',
  server: { port: 5173, strictPort: true, host: '127.0.0.1' },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'chrome120',
    rolldownOptions: {
      output: {
        // The renderer used to ship as one 678 kB chunk. React and the analytics/reporting
        // screens are split out so the first paint only pays for the shell.
        codeSplitting: {
          groups: [
            { name: 'vendor', test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/ },
            {
              name: 'analytics',
              test: /src[\\/]pages[\\/]analytics[\\/]/,
              // Keep shared UI modules in the entry chunk; otherwise the analytics chunk captures
              // them recursively and index.html preloads it on first paint, defeating the split.
              includeDependenciesRecursively: false,
            },
          ],
        },
      },
    },
  },
});
