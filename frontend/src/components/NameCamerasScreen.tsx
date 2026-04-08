import { useEffect, useState } from "react";
import { updateCameraName } from "../api/client";
import { apiUrl } from "../lib/backend";
import type { Camera } from "../types";

// Suggestion chips covering both home and small-business vocabularies
// (per the camera-naming UX memo in current-state.md §7 — we don't ask
// "home or business" because that violates zero-config). Users can
// also free-type anything; chips are an accelerator for the 90% case.
const SUGGESTIONS = [
  "Front door",
  "Driveway",
  "Back yard",
  "Garage",
  "Porch",
  "Side yard",
  "Living room",
  "Kitchen",
  "Office",
  "Basement",
  "Hallway",
  "Parking lot",
  "Register",
  "Aisle 1",
  "Aisle 2",
  "Storeroom",
  "Back door",
  "Lobby",
];

// ---------------------------------------------------------------------------
// NameCamerasScreen — the bulk rename experience.
//
// Vertical stack of camera cards. Each card shows camera identity
// (hostname, IP, MAC OUI manufacturer), a text input pre-filled with
// the current name or "Camera N", a row of suggestion chips, and a
// Save All button at the bottom that POSTs renames in parallel.
//
// Deliberately does NOT show live thumbnails yet — the snapshot
// endpoint (GET /api/cameras/{id}/thumbnail.jpg) doesn't exist. When
// it lands, swap the placeholder <Thumb/> for a real <img>.
// ---------------------------------------------------------------------------
export function NameCamerasScreen({
  cameras,
  onDone,
}: {
  cameras: Camera[];
  onDone: () => void;
}) {
  const [names, setNames] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    cameras.forEach((c, idx) => {
      init[c.id] = c.name ?? `Camera ${idx + 1}`;
    });
    return init;
  });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      // POST renames in parallel. If any fail, keep the successful ones
      // and surface the error inline — partial success is valid.
      const results = await Promise.allSettled(
        cameras.map((c) => {
          const desired = names[c.id]?.trim();
          if (!desired || desired === c.name) {
            return Promise.resolve(null);
          }
          return updateCameraName(c.id, desired);
        }),
      );
      const failures = results.filter((r) => r.status === "rejected");
      if (failures.length > 0) {
        setSaveError(
          `${failures.length} camera${failures.length > 1 ? "s" : ""} could not be renamed`,
        );
        return;
      }
      onDone();
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex-1 flex flex-col bg-[#0a0a0a]">
      {/* Topbar */}
      <div className="flex items-center justify-between px-5 h-12 bg-[#1a1a1a] border-b border-[#333] shrink-0">
        <span className="text-[#ddd] font-bold text-[15px]">Name your cameras</span>
        <button
          onClick={onDone}
          className="px-3 py-1.5 text-[#888] hover:text-[#ddd] text-xs transition-colors"
        >
          Skip
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-[780px] mx-auto px-6 py-10">
          <h1 className="text-[24px] font-bold text-[#ededed] m-0 mb-2">
            What should we call each camera?
          </h1>
          <p className="text-[14px] text-[#888] m-0 mb-8">
            Tap a suggestion or type your own. Names show up everywhere — in the
            Inbox, the Dashboard, and any clips you save.
          </p>

          <div className="flex flex-col gap-5">
            {cameras.map((c, idx) => (
              <CameraCard
                key={c.id}
                camera={c}
                index={idx}
                value={names[c.id] ?? ""}
                onChange={(next) =>
                  setNames((prev) => ({ ...prev, [c.id]: next }))
                }
              />
            ))}
          </div>

          {saveError && (
            <div className="mt-6 px-4 py-3 rounded-md bg-[rgba(239,68,68,0.1)] border border-[rgba(239,68,68,0.3)] text-[13px] text-[#fca5a5]">
              {saveError}
            </div>
          )}

          <div className="mt-8 flex items-center gap-3">
            <button
              disabled={saving}
              onClick={handleSave}
              className="px-5 py-2.5 bg-[#2b4c1f] border border-[#3a6428] text-[#d9f5c4] text-[14px] font-semibold rounded-md hover:bg-[#355d24] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {saving ? "Saving…" : "Save names"}
            </button>
            <button
              onClick={onDone}
              disabled={saving}
              className="px-5 py-2.5 bg-transparent text-[#888] text-[14px] font-medium rounded-md hover:text-[#ddd] transition-colors disabled:opacity-40"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function CameraCard({
  camera,
  index,
  value,
  onChange,
}: {
  camera: Camera;
  index: number;
  value: string;
  onChange: (next: string) => void;
}) {
  const subtitle = [
    camera.manufacturer,
    camera.model,
    camera.hostname,
    camera.ip,
  ]
    .filter(Boolean)
    .join(" · ");

  // Resolve the snapshot URL once on mount. The endpoint
  // (/api/cameras/{id}/snapshot.jpg) returns the latest preview frame
  // buffered in the recorder's FrameBroadcaster, so this is free —
  // no new RTSP connection, no decode work.
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotError, setSnapshotError] = useState(false);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const url = await apiUrl(
          // Cache-buster so we get a fresh frame each time the
          // naming screen opens, instead of a stale one from a
          // previous visit.
          `/api/cameras/${camera.id}/snapshot.jpg?t=${Date.now()}`,
        );
        if (!cancelled) setSnapshotUrl(url);
      } catch {
        if (!cancelled) setSnapshotError(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [camera.id]);

  return (
    <div className="flex gap-5 p-5 rounded-xl border border-[#2a2a2a] bg-[rgba(255,255,255,0.015)]">
      <div
        className="relative w-[160px] h-[90px] rounded-md border border-[#333] shrink-0 overflow-hidden bg-[#050505]"
        style={
          snapshotUrl && !snapshotError
            ? undefined
            : {
                background:
                  "linear-gradient(135deg, rgba(255,255,255,0.02), rgba(0,0,0,0.3)), radial-gradient(circle at 30% 40%, #1f1f1f, #0a0a0a 70%)",
              }
        }
      >
        {snapshotUrl && !snapshotError ? (
          <img
            src={snapshotUrl}
            alt=""
            className="w-full h-full object-cover"
            onError={() => setSnapshotError(true)}
          />
        ) : (
          <svg
            className="absolute inset-0 m-auto w-6 h-6 opacity-[0.2]"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.4"
          >
            <rect x="2" y="6" width="15" height="12" rx="2" />
            <path d="M17 10 L22 7 L22 17 L17 14 Z" strokeLinejoin="round" />
          </svg>
        )}
        <span className="absolute bottom-1 left-1.5 text-[10px] font-semibold text-white bg-black/60 px-1.5 py-[1px] rounded-sm">
          Camera {index + 1}
        </span>
      </div>

      <div className="flex-1 min-w-0">
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={`Camera ${index + 1}`}
          className="w-full px-3 py-2 text-[16px] font-semibold text-[#ededed] bg-[#0a0a0a] border border-[#333] rounded-md focus:border-[#4ade80] focus:outline-none"
        />
        {subtitle && (
          <p className="text-[11px] text-[#666] mt-1.5 mb-3 truncate">
            {subtitle}
          </p>
        )}

        <div className="flex flex-wrap gap-1.5">
          {SUGGESTIONS.map((suggestion) => {
            const active = value === suggestion;
            return (
              <button
                key={suggestion}
                onClick={() => onChange(suggestion)}
                className={
                  active
                    ? "px-2.5 py-1 text-[11px] font-semibold rounded-full bg-[#2b4c1f] border border-[#3a6428] text-[#d9f5c4] transition-colors"
                    : "px-2.5 py-1 text-[11px] font-medium rounded-full bg-transparent border border-[#2a2a2a] text-[#888] hover:text-[#ddd] hover:border-[#444] transition-colors"
                }
              >
                {suggestion}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
