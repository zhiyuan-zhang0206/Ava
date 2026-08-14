import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The Vite dev server is served directly (no gateway reverse proxy) at its
// own root `/`, reachable over the private network. Bind 0.0.0.0 so the user's
// browser can reach it; HMR's WebSocket is same-origin and needs no special
// clientPort/protocol. `base: '/'` (the default) is correct for root serving.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    // strictPort: agent sets the port via `--port <P>` CLI flag and
    // registers the same P via ava.ui.show(name, P). If the requested
    // port is taken, Vite should fail loudly rather than pick a different
    // one and silently desync from the registration.
    strictPort: true,
  },
});
