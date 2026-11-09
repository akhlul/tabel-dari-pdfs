// @ts-check
import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';

const API = process.env.API_URL ?? 'http://127.0.0.1:8000';

// Static build; FastAPI serves dist/ in production. In dev, /api is proxied to FastAPI.
export default defineConfig({
  output: 'static',
  build: { format: 'file' },
  server: { port: 4321 },
  devToolbar: { enabled: false },
  vite: {
    plugins: [tailwindcss()],
    server: { proxy: { '/api': { target: API, changeOrigin: true } } },
    optimizeDeps: { exclude: ['pdfjs-dist'] },
  },
});
