import { useEffect, useState } from "react";
import { fetchSettings, updateSettings } from "../api/client";
import { isTauri } from "../lib/backend";
import type { Settings } from "../types";

const FPS_OPTIONS = [
  { value: "original", label: "Original (no re-encode, full quality)" },
  { value: "10", label: "10 fps (smooth)" },
  { value: "5", label: "5 fps (balanced)" },
  { value: "2", label: "2 fps (low storage)" },
  { value: "1", label: "1 fps (very low storage)" },
  { value: "0.5", label: "0.5 fps (minimum, time-lapse style)" },
];

export function SettingsModal({ onClose }: { onClose: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchSettings().then(setSettings);
  }, []);

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
        <h2 className="text-base font-bold text-[#ddd] mb-1">Recording Settings</h2>
        <p className="text-xs text-[#888] mb-5">
          Configure storage limits and recording quality
        </p>

        <div className="mb-4">
          <label className="block text-xs font-medium text-[#888] mb-1.5">
            Storage Budget
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
            Oldest recordings are automatically deleted when full
          </p>
        </div>

        <div className="mb-4">
          <label className="block text-xs font-medium text-[#888] mb-1.5">
            Recording Frame Rate
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
            Lower frame rates = much more storage. 1fps gives ~30× more time.
          </p>
        </div>

        <div className="mb-4">
          <label className="block text-xs font-medium text-[#888] mb-1.5">
            Segment Duration
          </label>
          <select
            value={settings.segment_duration_minutes}
            onChange={(e) =>
              setSettings({
                ...settings,
                segment_duration_minutes: parseInt(e.target.value),
              })
            }
            className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500"
          >
            <option value={5}>5 minutes</option>
            <option value={15}>15 minutes</option>
            <option value={30}>30 minutes</option>
            <option value={60}>1 hour</option>
          </select>
          <p className="text-[11px] text-[#555] mt-1.5">
            Recording is split into files of this length
          </p>
        </div>

        <div className="mb-4">
          <label className="block text-xs font-medium text-[#888] mb-1.5">
            Recordings Folder
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
