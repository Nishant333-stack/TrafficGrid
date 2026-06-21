import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsDir: "assets",
  },
  base: "/app/",
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/events": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/metrics": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/field": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/plans": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/workflow": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/reports": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/sla": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/audit": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/platform": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/models": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/integrations": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
});
