import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  base: "./",
  plugins: [tailwindcss()],
  build: {
    emptyOutDir: true,
    outDir: "../src/agent_filetree_memory/web/dist",
    reportCompressedSize: false,
    sourcemap: false,
  },
});
