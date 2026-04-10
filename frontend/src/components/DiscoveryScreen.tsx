import { useEffect, useState } from "react";
import { triggerScan } from "../api/client";
import type { Camera, ScanStatus } from "../types";
import { AuthModal } from "./AuthModal";
import { CameraRow } from "./CameraRow";
import { ManualAddCameraModal } from "./ManualAddCameraModal";

// ---------------------------------------------------------------------------
// DiscoveryScreen — unified first-run camera setup.
//
// Absorbs the old standalone ScanScreen: this one screen handles every
// pre-Home state a user passes through, so the first-run flow is
// one screen instead of three.
//
// State machine (driven by props, no local transitions):
//   connecting  : backend not reachable yet           → "Starting up…"
//   scanning    : scan in progress, zero cameras yet  → "Looking for cameras…"
//   empty       : scan done, zero cameras             → "No cameras found" + search-again + manual-add
//   found       : at least one camera discovered      → table of cameras with sign-in affordances
//
// Auto-advance is time-gated. The old screen auto-advanced the instant
// the first camera came online — which meant users with multiple
// cameras had the screen yanked out from under them while they were
// still signing in to the others. Now we wait a grace period after
// the first online camera appears so the user has a chance to sign in
// to the rest.
// ---------------------------------------------------------------------------

export function DiscoveryScreen({
  cameras,
  connected,
  scanStatus,
  onContinue,
}: {
  cameras: Camera[];
  connected: boolean;
  scanStatus: ScanStatus;
  onContinue: () => void;
}) {
  const [authCamera, setAuthCamera] = useState<Camera | null>(null);
  const [rescanning, setRescanning] = useState(false);
  const [showManualAdd, setShowManualAdd] = useState(false);
  // IDs of cameras currently in the backend applyAll cascade. The
  // backend sequentially probes each one, which can take 2–5s per
  // camera — without this optimistic state the user watches stale
  // "Needs login" badges linger while the cascade grinds through.
  const [signingInIds, setSigningInIds] = useState<Set<string>>(new Set());
  // Transient acknowledgement for manual-add. Submitting the "Add by
  // IP" modal used to close silently and the new camera would appear
  // on the next scan tick with zero feedback — the banner gives the
  // user an immediate "yes, that worked" signal.
  const [justAdded, setJustAdded] = useState<{ id: string; label: string } | null>(
    null,
  );

  const online = cameras.filter((c) => c.status === "online").length;
  const scanComplete = scanStatus.last_scan !== null;
  const isScanning = scanStatus.scanning || rescanning;

  // Derived state label. One source of truth so the splash and the
  // main layout always agree on what the app is doing.
  const phase: "connecting" | "scanning" | "empty" | "found" = !connected
    ? "connecting"
    : cameras.length === 0 && !scanComplete
      ? "scanning"
      : cameras.length === 0 && scanComplete
        ? "empty"
        : "found";

  // Clear optimistic "signing in" state once the backend cascade
  // catches up — either the camera flipped out of needs_auth (success
  // or still needs_auth with the wrong password, which the backend
  // silently leaves alone) or it's gone. A 25s safety timeout catches
  // the edge case where the cascade hangs on an unreachable camera.
  useEffect(() => {
    if (signingInIds.size === 0) return;
    const stillRelevant = new Set<string>();
    for (const id of signingInIds) {
      const cam = cameras.find((c) => c.id === id);
      if (cam && cam.status === "needs_auth") stillRelevant.add(id);
    }
    if (stillRelevant.size !== signingInIds.size) {
      setSigningInIds(stillRelevant);
      return;
    }
    const timeout = setTimeout(() => setSigningInIds(new Set()), 25_000);
    return () => clearTimeout(timeout);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameras, signingInIds]);

  // Auto-dismiss the "Added …" banner after a few seconds. Long
  // enough to register, short enough to get out of the way before
  // the user moves on to sign-in.
  useEffect(() => {
    if (!justAdded) return;
    const t = setTimeout(() => setJustAdded(null), 5_000);
    return () => clearTimeout(t);
  }, [justAdded]);

  const handleAuthSuccess = (
    _username: string,
    _password: string,
    applyAll: boolean,
  ) => {
    if (!applyAll || !authCamera?.manufacturer) return;
    // Mirror the backend's same-manufacturer cascade gate. We don't
    // fire any extra requests ourselves — the backend is already
    // doing the work — we just mask the latency in the UI.
    const siblings = cameras
      .filter(
        (c) =>
          c.id !== authCamera.id &&
          c.status === "needs_auth" &&
          c.manufacturer === authCamera.manufacturer,
      )
      .map((c) => c.id);
    if (siblings.length === 0) return;
    setSigningInIds((prev) => {
      const next = new Set(prev);
      for (const id of siblings) next.add(id);
      return next;
    });
  };

  const handleManualAdded = (camera: Camera) => {
    const label = camera.name || camera.ip;
    setJustAdded({ id: camera.id, label });
    handleRescan();
  };

  const handleRescan = async () => {
    setRescanning(true);
    try {
      await triggerScan();
    } finally {
      setRescanning(false);
    }
  };

  // Splash layout for connecting / scanning / empty. The "found"
  // phase drops this in favor of the table layout below.
  if (phase !== "found") {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-6 p-10 text-center">
        <div className="w-14 h-14 rounded-xl bg-[#222] border border-[#333] flex items-center justify-center">
          <svg
            className="w-7 h-7 text-[#888]"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            viewBox="0 0 24 24"
          >
            <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" />
            <circle cx="12" cy="13" r="4" />
          </svg>
        </div>

        <h1 className="text-[22px] font-bold tracking-tight text-[#ddd]">
          SimpleNVR
        </h1>

        <p className="text-sm text-[#888] min-h-[20px]">
          {phase === "connecting"
            ? "Starting up…"
            : phase === "scanning"
              ? "Looking for cameras on your network…"
              : "No cameras found on your network"}
        </p>

        {/* Progress bar — only shown in connecting/scanning states */}
        {phase !== "empty" && (
          <div className="w-48 h-[3px] bg-[#2a2a2a] rounded overflow-hidden">
            <div
              className="h-full bg-blue-500 rounded transition-all duration-500"
              style={{ width: phase === "connecting" ? "30%" : "70%" }}
            />
          </div>
        )}

        {phase === "empty" && (
          <div className="flex flex-col items-center gap-2 mt-2">
            <div className="flex gap-2">
              <button
                onClick={handleRescan}
                disabled={isScanning}
                className="px-6 py-2 bg-blue-500 text-white text-[13px] font-semibold rounded-md hover:bg-blue-600 transition-colors disabled:opacity-50"
              >
                {isScanning ? "Searching…" : "Search again"}
              </button>
              <button
                onClick={() => setShowManualAdd(true)}
                className="px-6 py-2 bg-[#222] border border-[#333] text-[#ddd] text-[13px] font-semibold rounded-md hover:bg-[#2a2a2a] transition-colors"
              >
                Add by IP address
              </button>
            </div>
            <p className="text-[11px] text-[#555] max-w-xs leading-relaxed mt-2">
              Make sure your cameras are powered on and connected to the same
              network as this computer.
            </p>
            <button
              onClick={onContinue}
              className="text-xs text-[#666] hover:text-[#ddd] transition-colors mt-3"
            >
              ← Done
            </button>
          </div>
        )}

        {showManualAdd && (
          <ManualAddCameraModal
            onClose={() => setShowManualAdd(false)}
            onAdded={handleManualAdded}
          />
        )}
      </div>
    );
  }

  // "found" phase — the table layout with per-camera sign-in rows.
  return (
    <div className="flex-1 flex flex-col p-8 max-w-[840px] mx-auto w-full gap-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-bold text-[#ddd]">Your cameras</h1>
          <p className="text-[13px] text-[#888] mt-1">
            {cameras.length === 1
              ? "Found 1 camera. Click “Needs login” to sign in."
              : `Found ${cameras.length} cameras. Click “Needs login” on each one to sign in.`}
          </p>
        </div>
        {/* Escape hatch: manual add for cameras that didn't
            auto-discover. Subtle button — most users never need this,
            but it's always visible so users who DO need it can find it. */}
        <button
          onClick={() => setShowManualAdd(true)}
          className="text-xs text-[#888] hover:text-[#ddd] transition-colors whitespace-nowrap shrink-0 mt-1"
        >
          + Add by IP address
        </button>
      </div>

      {/* Manual-add acknowledgement. Auto-dismisses after 5s so it
          doesn't pile up if the user adds several cameras in a row. */}
      {justAdded && (
        <div className="px-3 py-2 bg-blue-500/10 border border-blue-500/30 rounded text-xs text-blue-200 flex items-center gap-2">
          <svg
            className="w-3.5 h-3.5 shrink-0"
            fill="none"
            stroke="currentColor"
            strokeWidth={2.5}
            viewBox="0 0 24 24"
          >
            <path d="M20 6 9 17l-5-5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Added <span className="font-mono">{justAdded.label}</span> — checking
          the connection…
        </div>
      )}

      {/* Table */}
      <div className="border border-[#333] rounded-md overflow-hidden">
        <div className="flex items-center px-4 py-2 bg-[#222] text-[11px] uppercase tracking-wide text-[#888] font-semibold gap-4">
          <div className="w-24 shrink-0">Brand</div>
          <div className="flex-1">Camera</div>
          <div className="w-[130px] shrink-0 hidden sm:block">Address</div>
          <div className="w-20 shrink-0 hidden md:block">Resolution</div>
          <div className="w-[100px] shrink-0">Status</div>
        </div>

        {cameras.map((cam) => (
          <CameraRow
            key={cam.id}
            camera={cam}
            onAuthClick={() => setAuthCamera(cam)}
            signingIn={signingInIds.has(cam.id)}
            highlight={justAdded?.id === cam.id}
          />
        ))}
      </div>

      {/* Actions */}
      <div className="flex items-center justify-between gap-2">
        <button
          onClick={handleRescan}
          disabled={isScanning}
          className="px-5 py-2 bg-[#222] border border-[#333] text-[#ddd] text-[13px] font-semibold rounded-md hover:bg-[#2a2a2a] transition-colors disabled:opacity-50"
        >
          {isScanning ? "Searching…" : "Search again"}
        </button>
        {online > 0 && (
          <button
            onClick={onContinue}
            className="px-5 py-2 bg-[#2b4c1f] border border-[#3a6428] text-[#d9f5c4] text-[13px] font-semibold rounded-md hover:bg-[#355d24] transition-colors"
          >
            Continue to live view →
          </button>
        )}
        {online === 0 && (
          <button
            onClick={onContinue}
            className="text-xs text-[#666] hover:text-[#ddd] transition-colors"
          >
            ← Done
          </button>
        )}
      </div>

      {authCamera && (
        <AuthModal
          camera={authCamera}
          onClose={() => setAuthCamera(null)}
          onSuccess={handleAuthSuccess}
        />
      )}

      {showManualAdd && (
        <ManualAddCameraModal
          onClose={() => setShowManualAdd(false)}
          onAdded={handleManualAdded}
        />
      )}
    </div>
  );
}
