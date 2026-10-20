import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { copyFileSync, mkdirSync } from "node:fs";
const root = fileURLToPath(new URL(".", import.meta.url));
export default defineConfig({
  root,
  plugins: [
    react(),
    {
      name: "local-opencascade-assets",
      buildStart() {
        const target = `${root}/public/occt`;
        mkdirSync(target, { recursive: true });
        for (const name of ["occt-import-js.js", "occt-import-js.wasm"])
          copyFileSync(
            `${root}/../node_modules/occt-import-js/dist/${name}`,
            `${target}/${name}`,
          );
      },
    },
  ],
  server: { port: 1420, strictPort: true, host: "127.0.0.1" },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        manualChunks: { three: ["three"], react: ["react", "react-dom"] },
      },
    },
  },
  test: { include: ["src/**/*.test.ts"], environment: "node" },
});
