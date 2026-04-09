import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Silence the EPIPE / ECONNRESET noise that backend or go2rtc
// restarts produce on the underlying proxy sockets, but let
// genuine errors (connection refused to a not-yet-running upstream,
// DNS errors, etc.) surface to the terminal — those are the kind
// of errors that turn into mysterious silent hangs in the browser
// otherwise. The earlier version of this helper was a blanket
// `swallow = () => {}` on every event, which hid an entire class
// of "go2rtc not running" failures during the 2026-04-09 churn.
const silenceProxy = (proxy: any) => {
  const swallowNoise = (err: any) => {
    if (err && (err.code === "EPIPE" || err.code === "ECONNRESET")) {
      return;
    }
    // eslint-disable-next-line no-console
    console.warn("[vite-proxy]", err?.message || err);
  };
  proxy.on("error", swallowNoise);
  proxy.on("proxyReqWs", (_proxyReq: any, _req: any, socket: any) => {
    socket.on("error", swallowNoise);
  });
  proxy.on("open", (socket: any) => {
    socket.on("error", swallowNoise);
  });
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3000,
    // Bind loopback-only. Without this, Vite defaults to 0.0.0.0
    // which makes the dev server (AND its go2rtc proxy at /g2r)
    // reachable from any device on the local network. Since the
    // /g2r proxy rewrites the Origin header to bypass go2rtc's
    // cross-site WebSocket protection, LAN-accessible dev mode
    // would let any other device on the /24 enumerate cameras
    // and stream live video via /g2r/api/streams and /g2r/api/ws.
    // Loopback-only dev keeps that attack surface gone.
    host: "127.0.0.1",
    proxy: {
      // go2rtc proxy — the frontend connects to /g2r/... for live
      // WebRTC/MSE WebSocket, HLS playlists, snapshot frames, etc.
      // Vite forwards to the go2rtc sidecar on 127.0.0.1:58581.
      // Two reasons this is a proxy instead of direct frontend →
      // go2rtc:
      //
      // 1. go2rtc rejects cross-origin WebSocket upgrades with HTTP
      //    403 (Cross-Site WebSocket Hijacking protection). Our
      //    frontend origin is http://localhost:3000 in dev and
      //    tauri://localhost in bundled mode — neither matches
      //    go2rtc's listen address, so every WebSocket was being
      //    rejected until this proxy was added (verified via curl
      //    2026-04-09). We REWRITE the Origin header to match
      //    go2rtc's own listen address so go2rtc accepts the
      //    upgrade.
      //
      // 2. Keeping the browser-facing origin single means no need
      //    to set go2rtc's api.origin to "*", which would expose
      //    go2rtc's admin API to any malicious web page the user
      //    visits while SimpleNVR is running.
      //
      // Must come BEFORE the /api rule below because /g2r is more
      // specific — Vite matches proxy paths top-to-bottom.
      "/g2r": {
        target: "http://127.0.0.1:58581",
        changeOrigin: true,
        ws: true,
        rewrite: (p) => p.replace(/^\/g2r/, ""),
        // Rewrite the Origin header so go2rtc sees a same-origin
        // request. Without this, go2rtc's strict WebSocket origin
        // check rejects us with 403 regardless of the rest of the
        // proxy configuration.
        headers: {
          Origin: "http://127.0.0.1:58581",
        },
        configure: silenceProxy,
      },
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
