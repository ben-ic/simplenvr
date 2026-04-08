import { useEffect, useState } from "react";
import { triggerScan } from "../api/client";
import type { ScanStatus } from "../types";

export function ScanScreen({
  scanStatus,
  camerasFound,
  connected,
  onContinue,
}: {
  scanStatus: ScanStatus;
  camerasFound: number;
  connected: boolean;
  onContinue: () => void;
}) {
  const [rescanning, setRescanning] = useState(false);
  const scanComplete = scanStatus.last_scan !== null;

  // Auto-advance to the discovery screen as soon as the scan finishes
  // and at least one camera has been found. No "Continue" button —
  // the user's goal is "see my cameras," and the scan screen doesn't
  // provide any information the discovery screen doesn't already
  // show. A short 1.2s delay gives the user a moment to register
  // "Found N cameras" before the screen changes, which turns a
  // jarring transition into a satisfying one.
  useEffect(() => {
    if (!scanComplete || camerasFound === 0) return;
    const t = setTimeout(() => onContinue(), 1200);
    return () => clearTimeout(t);
  }, [scanComplete, camerasFound, onContinue]);

  const handleRescan = async () => {
    setRescanning(true);
    try {
      await triggerScan();
    } finally {
      setRescanning(false);
    }
  };

  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-6 p-10 text-center">
      {/* Icon */}
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

      {!connected ? (
        <div className="flex items-center gap-2 text-sm text-[#888]">
          <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
          Connecting to server...
        </div>
      ) : (
        <>
          <p className="text-sm text-[#888] min-h-[20px]">
            {scanStatus.scanning || rescanning
              ? "Scanning your network for cameras..."
              : scanComplete && camerasFound === 0
              ? "Scan complete — no cameras found"
              : scanComplete
              ? "Scan complete"
              : "Preparing scan..."}
          </p>

          {/* Progress bar */}
          <div className="flex flex-col items-center gap-3">
            <div className="w-48 h-[3px] bg-[#2a2a2a] rounded overflow-hidden">
              <div
                className="h-full bg-blue-500 rounded transition-all duration-500"
                style={{
                  width: scanStatus.scanning || rescanning
                    ? "70%"
                    : scanComplete
                    ? "100%"
                    : "0%",
                }}
              />
            </div>

            {camerasFound > 0 && scanComplete && (
              <div className="flex items-center gap-2 text-[13px] text-green-500 font-medium">
                <svg
                  className="w-4 h-4"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={3}
                  viewBox="0 0 24 24"
                >
                  <polyline points="20 6 9 17 4 12" />
                </svg>
                Found {camerasFound} camera{camerasFound !== 1 ? "s" : ""}
              </div>
            )}
          </div>

          {/* Actions — only shown in the "nothing found" case.
              When cameras ARE found the ScanScreen auto-advances
              to the discovery screen after a short pause (see the
              useEffect above), so no button is needed. */}
          {scanComplete && camerasFound === 0 && (
            <div className="flex flex-col items-center gap-2 mt-2">
              <button
                onClick={handleRescan}
                disabled={rescanning}
                className="px-6 py-2 bg-blue-500 text-white text-[13px] font-semibold rounded-md hover:bg-blue-600 transition-colors disabled:opacity-50"
              >
                {rescanning ? "Scanning…" : "Scan again"}
              </button>
              <p className="text-[11px] text-[#555] max-w-xs leading-relaxed mt-1">
                Make sure your cameras are powered on and connected to the
                same network as this computer.
              </p>
            </div>
          )}
        </>
      )}
    </div>
  );
}
