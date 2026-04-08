import { useEffect, useState } from "react";
import { triggerScan } from "../api/client";
import type { Camera } from "../types";
import { AuthModal } from "./AuthModal";
import { CameraRow } from "./CameraRow";
import { ManualAddCameraModal } from "./ManualAddCameraModal";

export function DiscoveryScreen({
  cameras,
  onContinue,
  autoAdvance = false,
}: {
  cameras: Camera[];
  onContinue: () => void;
  // When true (first-run onboarding), the screen auto-advances as soon
  // as any camera comes online. When false (user opened this screen
  // from a "Manage cameras" link on a later visit), the screen stays
  // put so the user can actually manage cameras — adding more, signing
  // in to ones that need credentials, etc.
  autoAdvance?: boolean;
}) {
  const [authCamera, setAuthCamera] = useState<Camera | null>(null);
  const [scanning, setScanning] = useState(false);
  const [showManualAdd, setShowManualAdd] = useState(false);

  const online = cameras.filter((c) => c.status === "online").length;

  useEffect(() => {
    if (autoAdvance && online > 0) {
      onContinue();
    }
  }, [autoAdvance, online, onContinue]);

  const handleRescan = async () => {
    setScanning(true);
    await triggerScan();
    setScanning(false);
  };

  return (
    <div className="flex-1 flex flex-col p-8 max-w-[840px] mx-auto w-full gap-6">
      <div className="flex items-start justify-between gap-4">
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
        {/* Escape hatch: manual add for cameras that didn't
            auto-discover. Subtle button — most users never need this,
            but it's always visible so users who DO need it can find it
            without hunting through menus. */}
        <button
          onClick={() => setShowManualAdd(true)}
          className="text-xs text-[#888] hover:text-[#ddd] transition-colors whitespace-nowrap shrink-0 mt-1"
        >
          + Add camera manually
        </button>
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
          In first-run (autoAdvance) mode the screen auto-advances
          the moment any camera comes online, so the only explicit
          controls are "Rescan" and a subtle "Skip" link. In revisit
          mode (user clicked "Manage cameras") the screen stays put
          and we surface an explicit "Done" button. */}
      <div className="flex items-center justify-between gap-2">
        <button
          onClick={onContinue}
          className="text-xs text-[#666] hover:text-[#ddd] transition-colors"
        >
          {autoAdvance ? "Skip to dashboard →" : "← Done"}
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

      {/* Manual add modal — escape hatch for cameras that didn't auto-discover */}
      {showManualAdd && (
        <ManualAddCameraModal
          onClose={() => setShowManualAdd(false)}
          onAdded={() => {
            // Trigger a rescan so the new camera row propagates via
            // the same event stream that auto-discovery uses.
            handleRescan();
          }}
        />
      )}
    </div>
  );
}
