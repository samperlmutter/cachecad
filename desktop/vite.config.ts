import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  plugins: [react()],
  server: { port: 1420, strictPort: true, host: "127.0.0.1" },
  optimizeDeps: {
    include: ["@tauri-apps/plugin-updater", "@tauri-apps/plugin-process"],
  },
  build: { outDir: "dist", emptyOutDir: true },
  test: { include: ["src/**/*.test.ts"], environment: "node" },
});
