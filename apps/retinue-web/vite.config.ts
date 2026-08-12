import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev server proxies API calls to the rooms adapter; the production build is
// served BY the adapter itself (same origin, no CORS).
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    proxy: {
      "/rooms": "http://127.0.0.1:8653",
      "/agents": "http://127.0.0.1:8653",
      "/health": "http://127.0.0.1:8653",
    },
  },
});
