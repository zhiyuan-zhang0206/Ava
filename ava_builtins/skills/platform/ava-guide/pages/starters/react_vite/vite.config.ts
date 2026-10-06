import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Build assets relative to the registered page URL's path prefix.
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    host: '0.0.0.0',
    strictPort: true,
  },
});
