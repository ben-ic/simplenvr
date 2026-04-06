import { useState } from "react";
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
  const needsAuth = cameras.filter((c) => c.status === "needs_auth").length;

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
          {cameras.length} camera{cameras.length !== 1 ? "s" : ""} detected.{" "}
          {online > 0 && `${online} connected`}
          {online > 0 && needsAuth > 0 && ", "}
          {needsAuth > 0 && `${needsAuth} need${needsAuth === 1 ? "s" : ""} credentials`}
          .
        </p>
      </div>

      {/* Table */}
      <div className="border border-[#333] rounded-md overflow-hidden">
        {/* Header */}
        <div className="flex items-center px-4 py-2 bg-[#222] text-[11px] uppercase tracking-wide text-[#888] font-semibold">
          <div className="w-24 shrink-0">Preview</div>
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

      {/* Actions */}
      <div className="flex gap-2 justify-end">
        <button
          onClick={handleRescan}
          disabled={scanning}
          className="px-5 py-2 bg-[#222] border border-[#333] text-[#ddd] text-[13px] font-semibold rounded-md hover:bg-[#2a2a2a] transition-colors disabled:opacity-50"
        >
          {scanning ? "Scanning..." : "Rescan"}
        </button>
        <button
          onClick={onContinue}
          disabled={online === 0}
          className="px-5 py-2 bg-blue-500 text-white text-[13px] font-semibold rounded-md hover:bg-blue-600 transition-colors disabled:opacity-50 disabled:bg-[#222] disabled:text-[#555]"
        >
          {online === 0
            ? "Connect a camera to continue"
            : `View ${online} camera${online !== 1 ? "s" : ""} →`}
        </button>
      </div>

      {/* Auth modal */}
      {authCamera && (
        <AuthModal camera={authCamera} onClose={() => setAuthCamera(null)} />
      )}
    </div>
  );
}
