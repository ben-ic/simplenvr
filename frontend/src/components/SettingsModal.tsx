import { useEffect, useRef, useState } from "react";
import {
  clearData,
  fetchCurrentRecordingsDir,
  fetchStorage,
  fetchDiskFree,
  fetchSettings,
  resetDatabase,
  updateSettings,
} from "../api/client";
import { isTauri } from "../lib/backend";
import type { Settings } from "../types";

// Plain-English primary label plus the actual fps value in parens.
// The descriptive adjective is for users who don't know what fps
// means; the number is for users who do and want to pick accurately.
const FPS_OPTIONS = [
  { value: "original", label: "Best — full motion (original fps)" },
  { value: "10", label: "High — smooth (10 fps)" },
  { value: "5", label: "Medium — balanced (5 fps)" },
  { value: "2", label: "Low — more days kept (2 fps)" },
  { value: "1", label: "Very low — a lot more days (1 fps)" },
  { value: "0.5", label: "Minimum — time-lapse (0.5 fps)" },
];

export function SettingsModal({ onClose }: { onClose: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [diskFreeGb, setDiskFreeGb] = useState<number | null>(null);
  const [currentRecordingsDir, setCurrentRecordingsDir] = useState<string>("");
  const [savedBytes, setSavedBytes] = useState<number>(0);

  // Refresh disk free space when the recordings path changes
  const pathRef = useRef<string | null | undefined>(undefined);
  const refreshDiskFree = (path?: string | null) => {
    fetchDiskFree(path)
      .then((d) => setDiskFreeGb(d.free_gb))
      .catch(() => {});
  };

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [s, dir, storage] = await Promise.all([
          fetchSettings(),
          fetchCurrentRecordingsDir(),
          fetchStorage(),
        ]);
        if (!cancelled) {
          setSettings(s);
          setCurrentRecordingsDir(dir);
          setSavedBytes(storage.used_bytes);
          refreshDiskFree(s.recordings_path);
        }
      } catch {
        // Backend still starting — retry after a short delay
        if (!cancelled) setTimeout(load, 1000);
      }
    };
    load();
    return () => { cancelled = true; };
  }, []);

  // Re-fetch disk free when recordings path changes
  useEffect(() => {
    if (!settings) return;
    if (pathRef.current === settings.recordings_path) return;
    pathRef.current = settings.recordings_path;
    refreshDiskFree(settings.recordings_path);
  }, [settings?.recordings_path]);

  const handleSave = async () => {
    if (!settings) return;
    setSaving(true);
    setError(null);
    try {
      await updateSettings(settings);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleBrowseRecordingsPath = async () => {
    // Folder picker only works inside the Tauri webview. In browser
    // dev mode the user has to type/paste a path manually.
    if (!isTauri()) return;
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selected = await open({
        directory: true,
        multiple: false,
        title: "Select recordings folder",
        defaultPath: settings?.recordings_path || undefined,
      });
      if (typeof selected === "string" && settings) {
        setSettings({ ...settings, recordings_path: selected });
        setError(null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const formatSavedSize = (bytes: number): string => {
    if (bytes <= 0) return "0 MB";
    const gb = bytes / (1024 ** 3);
    if (gb >= 1) return `${gb.toFixed(1)} GB`;
    const mb = bytes / (1024 ** 2);
    return `${Math.round(mb)} MB`;
  };

  if (!settings) {
    return (
      <div className="fixed inset-0 bg-black/65 flex items-center justify-center z-50">
        <div className="bg-[#1a1a1a] border border-[#333] rounded-lg p-6 w-[420px]">
          <p className="text-sm text-[#888]">Loading...</p>
        </div>
      </div>
    );
  }

  return (
    <div
      className="fixed inset-0 bg-black/65 flex items-center justify-center z-50"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="bg-[#1a1a1a] border border-[#333] rounded-lg p-6 w-[460px] max-w-[90vw]">
        <h2 className="text-base font-bold text-[#ddd] mb-1">Settings</h2>
        <p className="text-xs text-[#888] mb-5">
          How much space to use and how good the recordings should look.
        </p>

        <div className="mb-4">
          <label className="block text-xs font-medium text-[#888] mb-1.5">
            Space to use
          </label>
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={1}
              max={10000}
              step={1}
              value={settings.max_storage_gb}
              onChange={(e) =>
                setSettings({
                  ...settings,
                  max_storage_gb: parseFloat(e.target.value) || 0,
                })
              }
              className="w-24 px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500 tabular-nums"
            />
            <span className="text-sm text-[#888]">GB</span>
          </div>
          <p className="text-[11px] text-[#555] mt-1.5">
            When this fills up, the oldest recordings are replaced.
          </p>
          {diskFreeGb !== null &&
            settings.max_storage_gb > diskFreeGb && (
              <p className="text-[11px] text-amber-400 mt-1">
                Not enough disk space — only{" "}
                {Math.floor(diskFreeGb)} GB free.
              </p>
            )}
        </div>

        <div className="mb-4">
          <label className="block text-xs font-medium text-[#888] mb-1.5">
            Video quality
          </label>
          <select
            value={settings.recording_fps}
            onChange={(e) =>
              setSettings({ ...settings, recording_fps: e.target.value })
            }
            className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500"
          >
            {FPS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <p className="text-[11px] text-[#555] mt-1.5">
            Lower quality keeps a lot more video. "Minimum" fits roughly 30×
            more days than "Best".
          </p>
        </div>

        {/*
          "Segment Duration" control removed per content review: it's a
          knob the target user has no basis for picking, and product.md
          §Zero Configuration explicitly forbids this class of setting.
          The backend keeps whatever value is already in the DB (default
          1 minute, set in backend/models.py Settings.segment_duration_minutes);
          existing installations are unaffected because we send the
          current value back unchanged on save.
        */}

        <div className="mb-4">
          <label className="block text-xs font-medium text-[#888] mb-1.5">
            Save recordings to
          </label>
          <div className="flex gap-2">
            <input
              type="text"
              value={settings.recordings_path ?? ""}
              placeholder="Default (app data folder)"
              onChange={(e) =>
                setSettings({
                  ...settings,
                  recordings_path: e.target.value || null,
                })
              }
              className="flex-1 min-w-0 px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500 font-mono"
            />
            <button
              type="button"
              onClick={handleBrowseRecordingsPath}
              disabled={!isTauri()}
              title={
                isTauri()
                  ? "Open folder picker"
                  : "Folder picker is only available in the desktop app"
              }
              className="px-3 py-[7px] bg-[#222] border border-[#333] text-[#ddd] text-xs font-semibold rounded hover:bg-[#2a2a2a] disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Browse…
            </button>
            {settings.recordings_path && (
              <button
                type="button"
                onClick={() =>
                  setSettings({ ...settings, recordings_path: null })
                }
                title="Reset to default location"
                className="px-3 py-[7px] bg-[#222] border border-[#333] text-[#888] text-xs font-semibold rounded hover:bg-[#2a2a2a]"
              >
                Reset
              </button>
            )}
          </div>
          <p className="text-[11px] text-[#777] mt-2 leading-[1.5] break-all">
            Current directory: {currentRecordingsDir || "(loading...)"}
          </p>
          <p className="text-[11px] text-[#777] mt-1">
            Data saved there: {formatSavedSize(savedBytes)}
          </p>
          <p className="text-[11px] text-[#555] mt-1.5">
            Leave blank to use the default. Changing this restarts active
            recorders — existing recordings stay where they were written.
          </p>
        </div>

        <label className="flex items-center gap-2 text-sm text-[#ddd] mt-4 mb-5 cursor-pointer">
          <input
            type="checkbox"
            checked={settings.recording_enabled}
            onChange={(e) =>
              setSettings({ ...settings, recording_enabled: e.target.checked })
            }
            className="accent-blue-500"
          />
          Recording enabled
        </label>

        {/* Clear Data Section */}
        <div className="border-t border-[#333] pt-4 mt-4">
          <h3 className="text-sm font-bold text-[#ddd] mb-2">Clear Data</h3>
          <p className="text-xs text-[#888] mb-3">
            Delete all recordings and motion events. Your cameras and settings are kept.
          </p>
          <ClearDataButton />
        </div>

        {/* Factory Reset Section */}
        <div className="border-t border-[#333] pt-4 mt-4">
          <h3 className="text-sm font-bold text-[#ddd] mb-2">Factory Reset</h3>
          <p className="text-xs text-[#888] mb-3">
            Permanently delete all cameras, recordings, and motion events. This cannot be undone.
          </p>
          <ResetButton />
        </div>

        {error && (
          <div className="mb-3 px-3 py-2 bg-red-950/50 border border-red-900 rounded text-xs text-red-300">
            {error}
          </div>
        )}

        <div className="flex gap-2">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 bg-[#222] border border-[#333] text-[#ddd] text-[13px] font-semibold rounded-md hover:bg-[#2a2a2a]"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="flex-1 px-4 py-2 bg-blue-500 text-white text-[13px] font-semibold rounded-md hover:bg-blue-600 disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ClearDataButton — wipe recordings/events, keep cameras and settings.
// ---------------------------------------------------------------------------
function ClearDataButton() {
  const [confirmStep, setConfirmStep] = useState<0 | 1 | 2>(0);
  const [clearing, setClearing] = useState(false);

  const handleClear = async () => {
    if (confirmStep === 0) {
      setConfirmStep(1);
      return;
    }
    setConfirmStep(2);
    setClearing(true);
    try {
      await clearData();
      window.location.reload();
    } catch (err) {
      alert(`Clear failed: ${err instanceof Error ? err.message : String(err)}`);
      setClearing(false);
      setConfirmStep(0);
    }
  };

  if (confirmStep === 0) {
    return (
      <button
        type="button"
        onClick={handleClear}
        className="px-3 py-2 bg-orange-700 hover:bg-orange-600 text-white text-xs font-semibold rounded border-none cursor-pointer"
      >
        Clear Data
      </button>
    );
  }

  if (confirmStep === 1) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-xs text-orange-300">
          This will delete all recordings and motion events. Are you sure?
        </span>
        <button
          type="button"
          onClick={() => setConfirmStep(0)}
          className="px-2 py-1 bg-gray-600 hover:bg-gray-500 text-white text-xs font-semibold rounded border-none cursor-pointer"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleClear}
          className="px-2 py-1 bg-orange-700 hover:bg-orange-600 text-white text-xs font-semibold rounded border-none cursor-pointer"
        >
          Yes, Clear Data
        </button>
      </div>
    );
  }

  // confirmStep === 2 (clearing)
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-orange-300">
        {clearing ? "Clearing recordings and events..." : "Done"}
      </span>
      {clearing && (
        <div className="w-3 h-3 border border-orange-400 border-t-transparent rounded-full animate-spin"></div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ResetButton — factory reset button with double confirmation.
// ---------------------------------------------------------------------------
function ResetButton() {
  const [confirmStep, setConfirmStep] = useState<0 | 1 | 2 | 3>(0);
  const [resetting, setResetting] = useState(false);

  const handleReset = async () => {
    if (confirmStep === 0) {
      setConfirmStep(1);
      return;
    }

    if (confirmStep === 1) {
      setConfirmStep(2);
      return;
    }

    if (confirmStep === 2) {
      setConfirmStep(3);
      setResetting(true);
      try {
        await resetDatabase();
        // Force page reload to restart the app
        window.location.reload();
      } catch (err) {
        alert(`Reset failed: ${err instanceof Error ? err.message : String(err)}`);
        setResetting(false);
        setConfirmStep(0);
      }
      return;
    }
  };

  const handleCancel = () => {
    setConfirmStep(0);
  };

  if (confirmStep === 0) {
    return (
      <button
        type="button"
        onClick={handleReset}
        className="px-3 py-2 bg-red-600 hover:bg-red-500 text-white text-xs font-semibold rounded border-none cursor-pointer"
      >
        Factory Reset
      </button>
    );
  }

  if (confirmStep === 1) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-xs text-red-300">
          This will remove all your recordings. Are you sure?
        </span>
        <button
          type="button"
          onClick={handleCancel}
          className="px-2 py-1 bg-gray-600 hover:bg-gray-500 text-white text-xs font-semibold rounded border-none cursor-pointer"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleReset}
          className="px-2 py-1 bg-red-600 hover:bg-red-500 text-white text-xs font-semibold rounded border-none cursor-pointer"
        >
          Yes, I Understand
        </button>
      </div>
    );
  }

  if (confirmStep === 2) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-xs text-red-300">
          This will permanently delete all cameras, events, and recordings. Are you absolutely sure?
        </span>
        <button
          type="button"
          onClick={handleCancel}
          className="px-2 py-1 bg-gray-600 hover:bg-gray-500 text-white text-xs font-semibold rounded border-none cursor-pointer"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleReset}
          className="px-2 py-1 bg-red-600 hover:bg-red-500 text-white text-xs font-semibold rounded border-none cursor-pointer"
        >
          Yes, Delete Everything
        </button>
      </div>
    );
  }

  // confirmStep === 3 (resetting)
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-red-300">
        {resetting ? "Resetting database and recordings..." : "Reset complete"}
      </span>
      {resetting && (
        <div className="w-3 h-3 border border-red-400 border-t-transparent rounded-full animate-spin"></div>
      )}
    </div>
  );
}
