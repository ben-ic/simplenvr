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

  // Poll the snapshot endpoint every 2 seconds while the card is
  // mounted. The endpoint returns the latest preview frame buffered
  // in the recorder's FrameBroadcaster (zero new work, no new RTSP
  // connection). Polling is necessary because:
  //
  //   1. On brand-new recorders the broadcaster may not have a frame
  //      yet (first 1-3 seconds), so the first fetch can 503 while
  //      the next one succeeds. Without retry the thumbnail sticks
  //      on the placeholder until the user remounts the screen.
  //   2. Refreshing live is the right UX for a naming screen — the
  //      user should SEE what each camera is looking at right now,
  //      not a stale frame from the last visit.
  //
  // The 2s cadence matches the backend's `Cache-Control: max-age=2`
  // on the snapshot response, so we never waste a fetch.
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotReady, setSnapshotReady] = useState(false);
  useEffect(() => {
    // A needs_auth camera has no running recorder, so the snapshot
    // endpoint will 503 indefinitely — skip the poll entirely and
    // let the "Needs credentials" badge below render instead.
    if (camera.status === "needs_auth") return;

    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;

    const tick = async () => {
      try {
        const url = await apiUrl(
          `/api/cameras/${camera.id}/snapshot.jpg?t=${Date.now()}`,
        );
        if (!cancelled) setSnapshotUrl(url);
      } catch {
        // apiUrl itself shouldn't really fail, but don't blow up the
        // poll loop if it does — the next tick will retry.
      }
    };

    tick();
    timer = setInterval(tick, 2000);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [camera.id, camera.status]);

  return (
    <div className="flex gap-5 p-5 rounded-xl border border-[#2a2a2a] bg-[rgba(255,255,255,0.015)]">
      <div
        className="relative w-[160px] h-[90px] rounded-md border border-[#333] shrink-0 overflow-hidden bg-[#050505]"
        style={
          snapshotReady
            ? undefined
            : {
                background:
                  "linear-gradient(135deg, rgba(255,255,255,0.02), rgba(0,0,0,0.3)), radial-gradient(circle at 30% 40%, #1f1f1f, #0a0a0a 70%)",
              }
        }
      >
        {/* Render the <img> whenever we have a URL, even if a previous
            fetch failed — the poll tick above will try again every 2s
            and the <img> will transparently swap when one succeeds.
            snapshotReady tracks whether we've ever had a successful
            load, which controls whether the placeholder gradient is
            visible underneath. */}
        {snapshotUrl && (
          <img
            src={snapshotUrl}
            alt=""
            className="w-full h-full object-cover"
            onLoad={() => setSnapshotReady(true)}
          />
        )}
        {!snapshotReady && camera.status !== "needs_auth" && (
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
        {!snapshotReady && camera.status === "needs_auth" && (
          // A needs_auth camera can never produce a snapshot — the
          // recorder won't start until the user authenticates. Show
          // an explicit badge instead of an ambiguous placeholder so
          // the user understands why this card is blank.
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 px-2 text-center">
            <svg
              className="w-4 h-4 text-[#f59e0b] opacity-80"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="3" y="11" width="18" height="11" rx="2" />
              <path d="M7 11V7a5 5 0 0 1 10 0v4" />
            </svg>
            <span className="text-[9px] font-semibold uppercase tracking-wide text-[#f59e0b]">
              Needs credentials
            </span>
          </div>
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
