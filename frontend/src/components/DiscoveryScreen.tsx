import { useEffect, useState } from "react";
import { triggerScan } from "../api/client";
import type { Camera } from "../types";
import { AuthModal } from "./AuthModal";
import { CameraRow } from "./CameraRow";

export function DiscoveryScreen({
  cameras,
  onContinue,
}: {
  cameras: Camera[];
  onContinue: () => void;
}) {
  const [authCamera, setAuthCamera] = useState<Camera | null>(null);
  const [scanning, setScanning] = useState(false);

  const online = cameras.filter((c) => c.status === "online").length;

  // Auto-advance to the dashboard the moment ANY camera comes online.
  // The discovery screen's purpose is to let the user sign in to
  // cameras that need credentials; once at least one camera is
  // streaming, there's no reason to keep them on this screen — the
  // dashboard is where the value lives. The user can always come
  // back to the discovery screen via the "Cameras" button in the
  // dashboard topbar if they want to add more cameras later.
  useEffect(() => {
    if (online > 0) {
      onContinue();
    }
  }, [online, onContinue]);

  const handleRescan = async () => {
    setScanning(true);
    await triggerScan();
    setScanning(false);
  };

  return (
    <div className="flex-1 flex flex-col p-8 max-w-[840px] mx-auto w-full gap-6">
      <div>
        <h1 className="text-lg font-bold text-[#ddd]">Cameras Found</h1>
        <p className="text-[13px] text-[#888] mt-1">
          {cameras.length === 0
            ? "Looking for cameras on your network…"
            : cameras.length === 1
            ? "1 camera detected. Click “Needs Login” to connect it."
            : `${cameras.length} cameras detected. Click “Needs Login” on each one to connect.`}
        </p>
      </div>

      {/* Table */}
      <div className="border border-[#333] rounded-md overflow-hidden">
        {/* Header */}
        <div className="flex items-center px-4 py-2 bg-[#222] text-[11px] uppercase tracking-wide text-[#888] font-semibold gap-4">
          <div className="w-24 shrink-0">Brand</div>
          <div className="flex-1">Camera</div>
          <div className="w-[130px] shrink-0 hidden sm:block">IP Address</div>
          <div className="w-20 shrink-0 hidden md:block">Resolution</div>
          <div className="w-[100px] shrink-0">Status</div>
        </div>

        {cameras.length === 0 ? (
          <div className="px-4 py-8 text-center text-[13px] text-[#555]">
            No cameras found on your network
          </div>
        ) : (
          cameras.map((cam) => (
            <CameraRow
              key={cam.id}
              camera={cam}
              onAuthClick={() => setAuthCamera(cam)}
            />
          ))
        )}
      </div>

      {/* Actions.
          No "Continue" button — the screen auto-advances to the
          dashboard the moment any camera comes online (see the
          useEffect above). The only explicit action here is
          "Rescan" for users whose cameras didn't show up on the
          first pass, plus a subtle "Skip to dashboard" link for
          users who want to bypass the sign-in step (they can come
          back later via the Cameras menu). */}
      <div className="flex items-center justify-between gap-2">
        <button
          onClick={onContinue}
          className="text-xs text-[#666] hover:text-[#ddd] transition-colors"
        >
          Skip to dashboard →
        </button>
        <button
          onClick={handleRescan}
          disabled={scanning}
          className="px-5 py-2 bg-[#222] border border-[#333] text-[#ddd] text-[13px] font-semibold rounded-md hover:bg-[#2a2a2a] transition-colors disabled:opacity-50"
        >
          {scanning ? "Scanning…" : "Rescan network"}
        </button>
      </div>

      {/* Auth modal */}
      {authCamera && (
        <AuthModal camera={authCamera} onClose={() => setAuthCamera(null)} />
      )}
    </div>
  );
}
