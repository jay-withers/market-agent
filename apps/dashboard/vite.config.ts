import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: {
    // Sourcemaps would roughly double the image for a dashboard nobody
    // debugs from production.
    sourcemap: false,
  },
  server: {
    host: true,
    port: 5173,
  },
});
