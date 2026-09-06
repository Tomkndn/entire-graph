import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built assets go to ../static/, which FastAPI serves at / (SPEC.md).
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
