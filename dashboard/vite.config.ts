import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');
  const backend = env.IOT_DASHBOARD_BACKEND || 'http://127.0.0.1:8000';
  const proxy = {
    '/api': { target: backend },
    '/ws': { target: backend, ws: true },
  };
  return {
    plugins: [react()],
    // The contrast test reads the real stylesheet text (styles.css?raw); by default Vitest swaps CSS for an empty string.
    test: { css: { include: [/styles\.css/] } },
    server: { host: 'localhost', port: 5173, strictPort: true, proxy },
    preview: { host: 'localhost', port: 5173, strictPort: true, proxy },
  };
});
