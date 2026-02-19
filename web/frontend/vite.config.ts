import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:9035",
      "/ws": { target: "ws://localhost:9035", ws: true },
      "/docs": "http://localhost:9035",
      "/metrics": "http://localhost:9035",
    },
  },
});
