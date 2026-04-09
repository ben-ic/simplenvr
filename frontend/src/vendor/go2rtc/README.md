# Vendored go2rtc web components

`video-rtc.js` and `video-stream.js` are copied verbatim from go2rtc
v1.9.14 (MIT-licensed). They provide the `<video-stream>` custom
element that implements the WebRTC → MSE → HLS → MJPEG fallback
chain for browser live-stream playback against a go2rtc server.

**Why vendored instead of loaded dynamically from go2rtc:**

go2rtc serves `Access-Control-Allow-Origin: *` on its `/api/*`
endpoints but NOT on its static files (including these JS modules).
Loading them cross-origin via `<script type="module">` from our
frontend origin (localhost:3000 in dev, tauri://localhost in
bundled mode) is blocked by the browser because the ES module
spec requires CORS for cross-origin modules regardless of the
`<script>` tag's origin. Vendoring puts them same-origin with our
frontend code, avoiding the CORS requirement entirely.

**License**: MIT (inherited from go2rtc).

**Do not modify** — these are vendor files. If go2rtc ships a
newer version and we want the updates, re-download from
`http://127.0.0.1:58581/video-stream.js` and `/video-rtc.js`
against the appropriate go2rtc version.

**Consumed by**: `frontend/src/components/CameraTile.tsx` imports
`video-stream.js` for its side effect of registering the
`<video-stream>` custom element globally via
`customElements.define()`.
