import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During local development the Vite dev server proxies API calls to the
// FastAPI backend so the frontend can use same-origin "/api" paths.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
