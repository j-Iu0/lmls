import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
const target = 'http://127.0.0.1:8080';
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'backend/lmls_studio/studio', emptyOutDir: true },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target,
        changeOrigin: true,
        ws: true,
        configure(proxy) {
          // Translate only this trusted dev origin. The backend's origin checks stay intact.
          const origin = (
            request: { setHeader(name: string, value: string): void },
            incoming: { headers: { origin?: string } },
          ) => {
            if (
              ['http://127.0.0.1:5173', 'http://localhost:5173'].includes(
                incoming.headers.origin ?? '',
              )
            ) request.setHeader('Origin', target);
          };
          proxy.on('proxyReq', origin);
          proxy.on('proxyReqWs', origin);
        },
      },
    },
  },
});
