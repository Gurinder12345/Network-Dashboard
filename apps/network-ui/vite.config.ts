import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server only: forward /api to a local network-api (for example a kubectl port-forward),
// so development uses the same relative /api paths as the Traefik-routed production build.
const devApiTarget = process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8081";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target: devApiTarget,
        changeOrigin: true,
      },
    },
  },
});
