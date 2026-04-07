import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Silence ALL events on a proxy + its underlying sockets so a backend
// restart doesn't spam the terminal with harmless EPIPE errors.
const silenceProxy = (proxy: any) => {
  const swallow = () => {};
  proxy.on("error", swallow);
  proxy.on("proxyReq", swallow);
  proxy.on("proxyReqWs", (_proxyReq: any, _req: any, socket: any) => {
    socket.on("error", swallow);
  });
  proxy.on("open", (socket: any) => {
    socket.on("error", swallow);
  });
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: "http://localhost:57321",
        changeOrigin: true,
        configure: silenceProxy,
      },
      "/ws": {
        target: "ws://localhost:57321",
        ws: true,
        configure: silenceProxy,
      },
    },
  },
});
