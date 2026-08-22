import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// During `npm run dev`, Vite serves the SPA on :5173 (or the next free
// port) and proxies /api/* to the backend (uvicorn) running on
// :8848. This keeps the dev loop tight: edits to .tsx files reflect
// in the browser instantly without rebuilding the wheel.
//
// In production the FastAPI app mounts the contents of `dist/` and
// there's no proxy to configure.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8848",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    target: "es2022",
  },
});
