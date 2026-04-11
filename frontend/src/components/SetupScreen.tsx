import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchDiskFree,
  fetchSettings,
  updateCameraName,
  updateSettings,
} from "../api/client";
import { apiUrl, isTauri } from "../lib/backend";
import type { Camera, Settings } from "../types";

// ---------------------------------------------------------------------------
// SetupScreen — the "Almost ready" moment between camera sign-in and the live
// dashboard. Shown exactly once, on first run (onboarding_completed === false).
//
// Two decisions live here:
//   1. Name each camera. The discovery table also supports inline rename, but
//      this is the place where a non-technical user actually does it, with a live thumbnail
//      of what the camera is seeing right now.
//   2. Pick a save location and storage budget. Both are optional — if the
//      user clicks Begin recording without touching anything, sensible
//      defaults (app data folder, 500 GB) are committed.
//
// Cross-platform: the folder picker is Tauri-native (plugin-dialog), which
// handles Windows/macOS/Linux paths transparently. In browser dev mode
// (where __TAURI_INTERNALS__ is undefined) the Browse button is disabled and
// the user can paste a path into the text field.
//
// Retention is a ROUGH estimate. Real bitrate is unknown until recording
// starts and StorageBanner has measured actual usage. The copy ("Rough
// math") and the italic caveat underneath make this explicit so the
// ~9 days number doesn't read as a promise.
// ---------------------------------------------------------------------------

// Placeholder estimate. Real per-camera bitrate replaces this after the
// first hour of recording (backend StorageBanner has the true number).
const GB_PER_CAM_PER_DAY = 14;

// Slider range, in GB. 50 GB floor so the estimate never collapses to
// hours; 2 TB ceiling covers a typical home + small-business install.
const BUDGET_MIN_GB = 50;
const BUDGET_MAX_GB = 2000;
const BUDGET_STEP_GB = 10;

// Suggestion chips, rotated across cameras so the same four words don't
// appear four times. Covers home + small-business vocabularies.
const CHIP_BANKS: string[][] = [
  ["Driveway", "Front door", "Garage", "Side gate"],
  ["Back yard", "Patio", "Pool", "Deck"],
  ["Front porch", "Walkway", "Entry", "Lobby"],
  ["Parking", "Back door", "Loading dock", "Side alley"],
  ["Living room", "Office", "Hallway", "Nursery"],
  ["Shop floor", "Register", "Aisle 1", "Storeroom"],
];

interface SetupScreenProps {
  cameras: Camera[];
  onDone: () => void;
  onBack: () => void;
}

export function SetupScreen({ cameras, onDone, onBack }: SetupScreenProps) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [budget, setBudget] = useState<number>(500);
  const [recordingsPath, setRecordingsPath] = useState<string | null>(null);
  const [diskFreeGb, setDiskFreeGb] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fetch settings once on mount. The setup screen is a one-shot view —
  // we read the current values, let the user edit them locally, and POST
  // the whole blob on Begin recording.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await fetchSettings();
        if (cancelled) return;
        setSettings(s);
        setBudget(s.max_storage_gb || 500);
        setRecordingsPath(s.recordings_path);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Refresh disk-free whenever the path changes. Lets the user see
  // "486 GB free on this drive" update as they pick different targets.
  useEffect(() => {
    if (!settings) return;
    let cancelled = false;
    fetchDiskFree(recordingsPath)
      .then((d) => {
        if (!cancelled) setDiskFreeGb(d.free_gb);
      })
      .catch(() => {
        // Non-fatal: the meta line just hides until next update
      });
    return () => {
      cancelled = true;
    };
  }, [recordingsPath, settings]);

  // ── Retention number with smooth tween ──
  const camCount = cameras.length;
  const rawRetentionDays =
    camCount > 0 ? budget / (GB_PER_CAM_PER_DAY * camCount) : 0;
  const [displayedDays, setDisplayedDays] = useState<number>(rawRetentionDays);
  const rafIdRef = useRef<number | null>(null);

  useEffect(() => {
    // Tween displayedDays toward rawRetentionDays. Feels like the number
    // is "settling" on the answer rather than jumping, which reinforces
    // the "rough estimate" framing.
    const step = () => {
      setDisplayedDays((prev) => {
        const diff = rawRetentionDays - prev;
        if (Math.abs(diff) < 0.04) {
          rafIdRef.current = null;
          return rawRetentionDays;
        }
        rafIdRef.current = requestAnimationFrame(step);
        return prev + diff * 0.2;
      });
    };
    rafIdRef.current = requestAnimationFrame(step);
    return () => {
      if (rafIdRef.current !== null) cancelAnimationFrame(rafIdRef.current);
    };
  }, [rawRetentionDays]);

  // ── Masthead date ── generated once on mount from the user's locale so
  // the "Volume I · No. 01 · Saturday, 11 April 2026" line respects whatever
  // regional formatting the OS is set to.
  const dateLabel = useMemo(() => {
    try {
      return new Date().toLocaleDateString(undefined, {
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric",
      });
    } catch {
      // Some locales/platforms throw on unusual options. Fall back to a
      // minimal label rather than crash the screen.
      return new Date().toDateString();
    }
  }, []);

  const handleBrowse = async () => {
    if (!isTauri()) return;
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selected = await open({
        directory: true,
        multiple: false,
        title: "Choose where to save recordings",
        defaultPath: recordingsPath || undefined,
      });
      if (typeof selected === "string") {
        setRecordingsPath(selected);
        setError(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleBegin = async () => {
    if (!settings) return;
    setSaving(true);
    setError(null);
    try {
      await updateSettings({
        ...settings,
        max_storage_gb: budget,
        recordings_path: recordingsPath,
        onboarding_completed: true,
      });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSaving(false);
    }
  };

  const formatDays = (d: number): string => {
    if (!Number.isFinite(d) || d <= 0) return "0";
    if (d < 10) return d.toFixed(1);
    return String(Math.round(d));
  };

  const formatBudget = (gb: number): string => {
    if (gb >= 1000) {
      const tb = gb / 1000;
      return `${tb % 1 === 0 ? tb.toFixed(0) : tb.toFixed(1)} TB`;
    }
    return `${Math.round(gb)} GB`;
  };

  // Amber the retention number when the user drags to something very
  // short. A warning without blocking — a non-technical user can still ship 1 GB of
  // storage if she wants, we just signal it's probably too little.
  const retentionTone =
    displayedDays < 2 ? "text-amber-400" : "text-blue-400";

  // Grid density — match the Home live grid convention: 2 cols for up to
  // 4 cameras, 3 cols above that. Single camera gets its own column so
  // the thumbnail is large.
  const gridColsClass =
    camCount <= 1
      ? "grid-cols-1"
      : camCount <= 4
        ? "grid-cols-2"
        : "grid-cols-3";

  return (
    <div className="flex-1 flex flex-col bg-[#0a0a0a] text-[#ededed] overflow-y-auto">
      {/* Topbar — mirrors production chrome */}
      <div className="flex items-center justify-between h-12 px-6 bg-[#1a1a1a] border-b border-[#2a2a2a] shrink-0">
        <span className="text-[15px] font-bold text-[#ededed] tracking-tight">
          SimpleNVR
        </span>
        <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-[#666]">
          First-time setup
        </span>
      </div>

      <div className="max-w-[760px] mx-auto px-8 py-16 w-full">
        {/* Masthead */}
        <div className="flex items-center gap-3.5 mb-7 text-[10px] font-bold uppercase tracking-[0.22em] text-[#666]">
          <span>Volume&nbsp;I</span>
          <span className="flex-1 h-px bg-[#1a1a1a]" />
          <span>No.&nbsp;01</span>
          <span className="flex-1 h-px bg-[#1a1a1a]" />
          <span>{dateLabel}</span>
          <span className="w-1.5 h-1.5 rounded-full bg-blue-500" />
        </div>

        {/* Hero */}
        <h1 className="text-[52px] font-bold tracking-[-0.035em] leading-[0.97] text-[#ededed] mb-[22px]">
          The first<br />day.
        </h1>
        <p className="text-[15px] leading-[1.7] text-[#a0a0a0] max-w-[540px] mb-14">
          All your cameras are online and recording. Before the first footage
          is written to disk, <em className="not-italic font-medium text-[#ededed]">two small decisions:</em>{" "}
          what to call each camera, and where the recordings should live.
        </p>

        {/* ── Section 01 · The cameras ── */}
        <SectionRule
          num="01"
          title="The cameras"
          meta={`${camCount} of ${camCount} online`}
        />
        <p className="text-[14px] leading-[1.7] text-[#a0a0a0] max-w-[580px] mb-7">
          Give each camera a name you&rsquo;ll recognize at a glance:{" "}
          <strong className="font-semibold text-[#ededed]">Driveway</strong>,{" "}
          <strong className="font-semibold text-[#ededed]">Back door</strong>,{" "}
          <strong className="font-semibold text-[#ededed]">Register</strong>,{" "}
          <strong className="font-semibold text-[#ededed]">Porch</strong>. Click
          any name to rename it.
        </p>
        <div className={`grid ${gridColsClass} gap-x-5 gap-y-6 mb-16`}>
          {cameras.map((cam, idx) => (
            <CameraCard
              key={cam.id}
              camera={cam}
              chips={CHIP_BANKS[idx % CHIP_BANKS.length]}
            />
          ))}
        </div>

        {/* ── Section 02 · The archive ── */}
        <SectionRule
          num="02"
          title="The archive"
          meta={
            diskFreeGb !== null
              ? `${Math.round(diskFreeGb)} GB free on this drive`
              : "Checking disk\u2026"
          }
        />
        <p className="text-[14px] leading-[1.7] text-[#a0a0a0] max-w-[580px] mb-7">
          Pick a drive, pick a budget. The newest recordings are always kept;
          the oldest are quietly replaced when the space fills up. No pruning,
          no file management, no surprises.
        </p>

        <div className="grid grid-cols-[1fr_1.3fr] gap-x-14 gap-y-8 mb-12">
          {/* Drive / path */}
          <div>
            <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-[#666] mb-2.5">
              Where it lives
            </div>
            <div className="flex items-baseline gap-2.5 border-b border-[#1a1a1a] pb-2">
              <input
                type="text"
                value={recordingsPath ?? ""}
                placeholder="Default (app data folder)"
                onChange={(e) =>
                  setRecordingsPath(e.target.value || null)
                }
                className="flex-1 min-w-0 bg-transparent border-none p-0 text-[14px] font-medium text-[#ededed] outline-none caret-blue-500 placeholder:text-[#555] font-mono"
              />
              <button
                type="button"
                onClick={handleBrowse}
                disabled={!isTauri()}
                title={
                  isTauri()
                    ? "Open folder picker"
                    : "Folder picker is only available in the desktop app"
                }
                className="text-[10px] font-bold uppercase tracking-[0.14em] text-[#666] hover:text-blue-400 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                Browse
              </button>
            </div>
          </div>

          {/* Budget slider */}
          <div>
            <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-[#666] mb-2.5">
              How much space
            </div>
            <div className="flex items-baseline justify-between gap-3 mb-3">
              <span className="text-[20px] font-bold text-[#ededed] tracking-[-0.015em] tabular-nums">
                {formatBudget(budget)}
              </span>
              <span className="text-[10px] font-medium uppercase tracking-[0.08em] text-[#444]">
                of 2 TB ceiling
              </span>
            </div>
            <BudgetSlider value={budget} onChange={setBudget} />
            <div className="flex justify-between mt-2.5 text-[9px] font-semibold uppercase tracking-[0.08em] text-[#444] tabular-nums">
              <span>50 GB</span>
              <span>500</span>
              <span>1 TB</span>
              <span>2 TB</span>
            </div>
          </div>
        </div>

        {/* Retention pull-quote */}
        <section className="max-w-[640px] mb-14">
          <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-[#666] mb-[18px] flex items-center gap-3.5">
            Rough math
            <span className="flex-1 h-px bg-[#2a2a2a]" />
          </div>
          <p className="text-[15px] leading-[1.75] text-[#a0a0a0] m-0">
            {camCount} {camCount === 1 ? "camera" : "cameras"}, {formatBudget(budget)}. That works out to about
            <span
              className={`block text-[96px] font-bold tracking-[-0.055em] leading-[0.9] my-[14px] tabular-nums transition-colors duration-300 ${retentionTone}`}
            >
              <span className="text-[56px] font-normal text-[#444] tracking-[-0.02em] mr-1">
                ~
              </span>
              {formatDays(displayedDays)}
              <span className="text-[24px] font-semibold text-[#666] tracking-[-0.005em] ml-2">
                days
              </span>
            </span>
            of continuous footage before the oldest is quietly replaced.
            <em className="italic text-[#666] text-[12px] block mt-3.5 leading-[1.55]">
              This is a rough estimate. The real figure sharpens once
              recording begins and we&rsquo;ve measured the actual bitrate of
              each camera.
            </em>
          </p>
        </section>

        {/* Error */}
        {error && (
          <div className="mb-4 px-4 py-3 bg-red-500/10 border border-red-500/30 rounded-md text-[13px] text-red-300">
            {error}
          </div>
        )}

        {/* Footer */}
        <div className="flex items-start justify-between gap-5 pt-8 border-t border-[#1a1a1a]">
          <div className="flex flex-col gap-1 max-w-[360px]">
            <button
              type="button"
              onClick={onBack}
              className="text-left text-[11px] font-bold uppercase tracking-[0.14em] text-[#666] hover:text-[#ededed] transition-colors bg-transparent border-none p-0 cursor-pointer"
            >
              &larr; Back to cameras
            </button>
            <span className="text-[12px] italic text-[#444] leading-[1.5]">
              Everything here can be changed later from Settings.
            </span>
          </div>
          <button
            type="button"
            onClick={handleBegin}
            disabled={saving}
            className="bg-blue-500 hover:bg-blue-400 text-white px-6 py-3 rounded-[3px] text-[11px] font-bold uppercase tracking-[0.14em] disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center gap-3 border-none cursor-pointer"
          >
            {saving ? "Saving\u2026" : "Begin recording"}
            <span aria-hidden="true">&rarr;</span>
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SectionRule — the numbered horizontal break that separates 01 / 02.
// Single responsibility so the main component stays readable.
// ---------------------------------------------------------------------------
function SectionRule({
  num,
  title,
  meta,
}: {
  num: string;
  title: string;
  meta: string;
}) {
  return (
    <div className="grid grid-cols-[auto_auto_1fr_auto] items-center gap-4 mb-[22px]">
      <span className="text-[11px] font-bold uppercase tracking-[0.2em] text-[#666] tabular-nums">
        {num}
      </span>
      <span className="text-[19px] font-bold tracking-[-0.015em] text-[#ededed]">
        {title}
      </span>
      <span className="h-px bg-[#1a1a1a]" />
      <span className="text-[11px] font-medium text-[#666] tabular-nums">
        {meta}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// CameraCard — one camera in the 2x2/3x3 grid. Owns its own snapshot
// polling and inline rename state.
//
// Snapshot polling: hits /api/cameras/{id}/snapshot.jpg every 2s. This
// matches NameCamerasScreen's cadence and the backend's Cache-Control:
// max-age=2 on the response, so we never waste a fetch. On first paint
// the recorder may not have a frame yet (503) — we simply retry next
// tick and the <img> transparently swaps when one arrives.
// ---------------------------------------------------------------------------
function CameraCard({
  camera,
  chips,
}: {
  camera: Camera;
  chips: string[];
}) {
  // `draft` only exists while editing. When idle we read camera.name
  // directly from props — no prop->state mirroring, no sync effect.
  const [draft, setDraft] = useState<string | null>(null);
  const editing = draft !== null;
  const [snapshotUrl, setSnapshotUrl] = useState<string | null>(null);
  const [snapshotReady, setSnapshotReady] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Snapshot polling. Skip for non-online cameras — the recorder isn't
  // running so the endpoint would 503 forever.
  useEffect(() => {
    if (camera.status !== "online") return;
    let cancelled = false;
    let timer: ReturnType<typeof setInterval> | null = null;

    const tick = async () => {
      try {
        const url = await apiUrl(
          `/api/cameras/${camera.id}/snapshot.jpg?t=${Date.now()}`,
        );
        if (!cancelled) setSnapshotUrl(url);
      } catch {
        // Transient — next tick will retry
      }
    };

    tick();
    timer = setInterval(tick, 2000);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [camera.id, camera.status]);

  const startEditing = () => {
    setDraft(camera.name ?? "");
    // Focus on next tick so the input has mounted
    setTimeout(() => inputRef.current?.select(), 0);
  };

  const commitEdit = async () => {
    const trimmed = (draft ?? "").trim();
    const previous = camera.name ?? "";
    setDraft(null);

    if (!trimmed || trimmed === previous) {
      // Nothing to save (empty or unchanged). The parent prop stays as
      // it was, so there's nothing to revert.
      return;
    }

    try {
      await updateCameraName(camera.id, trimmed);
      // Optimistic — the WS snapshot will reconcile if anything diverges.
    } catch {
      // Swallow silently. The display name stays on the previous value
      // (driven by camera.name from props), and the user can retry.
    }
  };

  const cancelEdit = () => {
    setDraft(null);
  };

  const applyChip = (chip: string) => {
    setDraft(chip);
    if (!editing) {
      setTimeout(() => inputRef.current?.focus(), 0);
    } else {
      inputRef.current?.focus();
    }
  };

  const hasName = (camera.name ?? "").length > 0;
  const displayName = camera.name ?? "";

  const subtitleParts = [camera.manufacturer, camera.model]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="flex flex-col">
      {/* Live feed thumbnail */}
      <div className="relative aspect-video rounded-lg overflow-hidden mb-3 border border-[#1a1a1a] bg-[#080808]">
        {/* Placeholder shimmer until the first frame lands */}
        {!snapshotReady && (
          <div
            className="absolute inset-0"
            style={{
              background:
                "linear-gradient(135deg, #161616 0%, #0a0a0a 70%)",
            }}
          />
        )}
        {snapshotUrl && (
          <img
            src={snapshotUrl}
            alt=""
            className="absolute inset-0 w-full h-full object-cover"
            onLoad={() => setSnapshotReady(true)}
          />
        )}
        {/* Bottom gradient vignette for the live tag readability */}
        <div className="absolute inset-0 bg-gradient-to-b from-transparent to-black/40 pointer-events-none" />
        {/* Live tag */}
        <div className="absolute top-2.5 left-2.5 flex items-center gap-1.5 px-1.5 py-0.5 rounded-sm bg-black/55 text-[9px] font-bold uppercase tracking-[0.14em] text-[#ededed] z-10">
          <span className="w-1 h-1 rounded-full bg-red-500 animate-pulse" />
          Live
        </div>
      </div>

      {/* Name — edit in place */}
      {editing ? (
        <input
          ref={inputRef}
          type="text"
          value={draft ?? ""}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitEdit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitEdit();
            if (e.key === "Escape") cancelEdit();
          }}
          placeholder="Name this camera"
          className="bg-transparent border-0 border-b border-blue-500 pb-0.5 text-[18px] font-bold tracking-[-0.012em] text-[#ededed] outline-none caret-blue-500 placeholder:text-[#444] placeholder:italic placeholder:font-medium"
          style={{ borderBottomWidth: "1.5px" }}
        />
      ) : (
        <button
          type="button"
          onClick={startEditing}
          className={`text-left text-[18px] tracking-[-0.012em] leading-[1.2] mb-1 flex items-center gap-2.5 bg-transparent border-none p-0 cursor-text transition-colors ${
            hasName
              ? "font-bold text-[#ededed] hover:text-blue-400"
              : "font-medium italic text-[#666] hover:text-[#888]"
          }`}
          title="Click to rename"
        >
          {hasName ? displayName : "Click to name"}
        </button>
      )}

      {/* Caption vs chips — chips while editing, brand+IP while idle */}
      {editing ? (
        <div className="flex flex-wrap gap-x-3.5 gap-y-2 mt-2">
          {chips.map((chip) => (
            <button
              key={chip}
              type="button"
              onMouseDown={(e) => {
                // MouseDown, not click — click would fire after blur and
                // the edit mode would already be torn down.
                e.preventDefault();
                applyChip(chip);
              }}
              className="bg-transparent border-0 p-0 text-[11px] font-medium text-[#666] hover:text-blue-400 transition-colors cursor-pointer"
            >
              {chip}
            </button>
          ))}
        </div>
      ) : (
        <div className="flex items-center gap-2.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-[#666]">
          {subtitleParts && (
            <>
              <span>{subtitleParts}</span>
              <span className="w-2.5 h-px bg-[#333]" />
            </>
          )}
          <span>{camera.ip}</span>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// BudgetSlider — hairline track with a single dot handle. Deliberately not
// chunky UI chrome; the number above is the hero, the control recedes.
// ---------------------------------------------------------------------------
function BudgetSlider({
  value,
  onChange,
}: {
  value: number;
  onChange: (v: number) => void;
}) {
  const pct =
    ((value - BUDGET_MIN_GB) / (BUDGET_MAX_GB - BUDGET_MIN_GB)) * 100;
  return (
    <div className="relative h-[22px] flex items-center cursor-ew-resize">
      {/* Hairline track */}
      <div className="absolute left-0 right-0 top-1/2 -translate-y-1/2 h-px bg-[#2a2a2a]" />
      {/* Fill */}
      <div
        className="absolute left-0 top-1/2 -translate-y-1/2 h-px bg-blue-500"
        style={{
          width: `${pct}%`,
          transition: "width 0.3s cubic-bezier(0.16, 1, 0.3, 1)",
        }}
      />
      {/* Handle */}
      <div
        className="absolute top-1/2 w-2.5 h-2.5 bg-[#ededed] rounded-full pointer-events-none"
        style={{
          left: `${pct}%`,
          transform: "translate(-50%, -50%)",
          boxShadow: "0 0 0 3px #0a0a0a",
          transition:
            "left 0.3s cubic-bezier(0.16, 1, 0.3, 1), transform 0.15s ease",
        }}
      />
      {/* Real input — covers the whole track area, invisible */}
      <input
        type="range"
        min={BUDGET_MIN_GB}
        max={BUDGET_MAX_GB}
        step={BUDGET_STEP_GB}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="absolute inset-0 w-full h-full opacity-0 cursor-ew-resize m-0"
        aria-label="Storage budget in gigabytes"
      />
    </div>
  );
}
