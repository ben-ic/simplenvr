import { useRef, useState } from "react";
import type { Camera } from "../types";
import { getBrandLogoUrl } from "../lib/brandLogos";
import { StatusBadge } from "./StatusBadge";
import { deleteCamera, updateCameraName } from "../api/client";

/**
 * One row on the discovery setup screen.
 *
 * Goal of the UI here: a non-technical user should be able to look
 * at this row and immediately match it to the physical camera on
 * their wall. That means surfacing every piece of identifying
 * information we could gather from the network — brand logo, brand
 * name, model (if we know it), DHCP hostname, IP, MAC OUI — laid
 * out so the most recognizable piece (the logo) is what their eye
 * lands on first.
 *
 * Where the fields come from:
 *   camera.manufacturer     — backend fingerprint identifier
 *   camera.model            — authenticated ONVIF GetDeviceInformation
 *                             (null until user signs in)
 *   camera.hostname         — reverse DNS on camera IP
 *   camera.mac_address      — ARP table lookup
 *   camera.device_type      — "camera" | "hub" | "hub_camera"
 *   camera.identification_source — "onvif" (definitive) | "fingerprint"
 *                                   (heuristic) | null
 */
export function CameraRow({
  camera,
  onAuthClick,
  signingIn = false,
  highlight = false,
}: {
  camera: Camera;
  onAuthClick: () => void;
  // When true, mask the StatusBadge with a "Signing in…" pill.
  // Used during the applyAll credential cascade so the user gets
  // instant feedback instead of watching "Needs login" linger while
  // the backend sequentially probes each sibling camera.
  signingIn?: boolean;
  // When true, apply a brief blue ring — used immediately after a
  // manual-add to draw the eye to the row that was just created.
  highlight?: boolean;
}) {
  // Inline "confirm to delete" state — two-click affordance avoids
  // both an accidental click on the trash icon wiping out a camera
  // and a modal-heavy experience for what is a one-shot operation.
  // The second click fires the delete; the button reverts to its
  // idle state after a short timeout if the user doesn't confirm.
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState("");
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const startEditing = (e: React.MouseEvent) => {
    e.stopPropagation();
    setEditValue(camera.name || "");
    setEditing(true);
    setTimeout(() => inputRef.current?.select(), 0);
  };

  const saveEdit = async () => {
    const trimmed = editValue.trim();
    if (!trimmed || trimmed === camera.name) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      await updateCameraName(camera.id, trimmed);
    } catch (err) {
      console.error("rename camera failed", err);
    } finally {
      setSaving(false);
      setEditing(false);
    }
  };

  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const handleDeleteClick = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!deleteArmed) {
      setDeleteArmed(true);
      setTimeout(() => setDeleteArmed(false), 3000);
      return;
    }
    setDeleting(true);
    try {
      await deleteCamera(camera.id);
      // The camera_deleted WebSocket event will drop the row from
      // useDiscovery's state; no local bookkeeping needed here.
    } catch (err) {
      console.error("delete camera failed", err);
      setDeleting(false);
      setDeleteArmed(false);
    }
  };
  const logoUrl = getBrandLogoUrl(camera.manufacturer);
  const isHub = camera.device_type === "hub";
  const isHubCamera = camera.device_type === "hub_camera";

  // Primary title: brand + model if we have both, else just brand,
  // else "Unknown Camera". A brand by itself ("Reolink") is already
  // useful — we don't need to hide it behind "Unknown" just because
  // the model isn't known yet.
  const title = (() => {
    if (camera.name) return camera.name;
    const parts = [camera.manufacturer, camera.model].filter(Boolean);
    if (parts.length > 0) return parts.join(" ");
    return "Unknown Camera";
  })();

  // Subtitle: use the DHCP hostname if it looks useful (not empty,
  // not literally "unknown" or "localhost"). If we don't have a
  // hostname but we DO have a brand, show the brand as the subtitle
  // to avoid a blank line. Otherwise fall back to dashes.
  const subtitle = (() => {
    if (isHub) return "Camera hub — sign in to see connected cameras";
    if (isHubCamera) return `Behind ${camera.parent_hub_id || "hub"}`;
    if (camera.hostname && !/^(unknown|localhost|\s*)$/i.test(camera.hostname)) {
      return camera.hostname;
    }
    if (camera.manufacturer && camera.name) return camera.manufacturer;
    return null;
  })();

  // Technical details line: IP + MAC OUI (first 3 bytes only — enough
  // to uniquely identify the device at a glance without leaking the
  // full MAC into screenshots). Rendered in a dim monospace line
  // under the subtitle.
  const macOui = camera.mac_address
    ? camera.mac_address.split(":").slice(0, 3).join(":")
    : null;

  return (
    <div
      className={`flex items-center gap-4 px-4 py-3 bg-[#1a1a1a] border-b border-[#333] hover:bg-[#222] transition-colors last:border-b-0 ${
        highlight ? "ring-2 ring-blue-500/60 ring-inset" : ""
      }`}
    >
      {/* Brand logo (or first-letter fallback) */}
      <div className="w-24 h-[54px] bg-[#0d0d0d] rounded flex items-center justify-center shrink-0 border border-[#262626]">
        {logoUrl ? (
          <img
            src={logoUrl}
            alt=""
            className="max-w-[75%] max-h-[75%] object-contain opacity-90"
          />
        ) : camera.manufacturer ? (
          <div className="text-xl font-bold text-[#666] tracking-tight">
            {camera.manufacturer.charAt(0)}
          </div>
        ) : (
          // True unknown — show a neutral camera icon placeholder
          <svg
            className="w-5 h-5 text-[#444]"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            viewBox="0 0 24 24"
          >
            <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" />
            <circle cx="12" cy="13" r="4" />
          </svg>
        )}
      </div>

      {/* Info column — title + subtitle + MAC OUI line */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          {editing ? (
            <input
              ref={inputRef}
              type="text"
              value={editValue}
              onChange={(e) => setEditValue(e.target.value)}
              onBlur={saveEdit}
              onKeyDown={(e) => {
                if (e.key === "Enter") saveEdit();
                if (e.key === "Escape") setEditing(false);
              }}
              disabled={saving}
              className="text-sm font-semibold text-[#ededed] bg-[#0a0a0a] border border-[#4ade80] rounded px-1.5 py-0.5 outline-none w-48"
              placeholder="Camera name"
            />
          ) : (
            <button
              onClick={startEditing}
              title="Click to rename"
              className="text-sm font-semibold text-[#ddd] truncate hover:text-white transition-colors cursor-text"
            >
              {title}
            </button>
          )}
          {isHub && (
            <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 bg-blue-500/15 text-blue-400 rounded">
              Hub
            </span>
          )}
          {camera.identification_source === "fingerprint" && !camera.model && (
            <span
              className="text-[9px] font-medium uppercase tracking-wider text-[#666]"
              title="Auto-detected from network signals. Will be confirmed after you sign in."
            >
              best guess
            </span>
          )}
        </div>
        {subtitle && (
          <div className="text-xs text-[#888] truncate mt-0.5">{subtitle}</div>
        )}
        {macOui && (
          <div className="text-[10px] text-[#555] font-mono mt-0.5 tabular-nums">
            {macOui}
          </div>
        )}
      </div>

      {/* IP */}
      <div className="text-[13px] text-[#888] font-mono w-[130px] shrink-0 hidden sm:block tabular-nums">
        {camera.ip}
      </div>

      {/* Resolution — only after ONVIF auth fills it in */}
      <div className="text-[13px] text-[#888] w-20 shrink-0 hidden md:block">
        {camera.resolutions[0] || "—"}
      </div>

      {/* Status */}
      <div className="shrink-0">
        {signingIn ? (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] font-semibold bg-blue-500/15 text-blue-300">
            <svg
              className="w-3 h-3 animate-spin"
              fill="none"
              stroke="currentColor"
              strokeWidth={2.5}
              viewBox="0 0 24 24"
            >
              <path d="M21 12a9 9 0 1 1-6.22-8.56" strokeLinecap="round" />
            </svg>
            Signing in…
          </span>
        ) : (
          <StatusBadge status={camera.status} onClick={onAuthClick} />
        )}
      </div>

      {/* Edit credentials — only shown for already-working cameras.
          For needs_auth cameras the StatusBadge itself is the
          affordance, so this would be a duplicate. The button opens
          the same AuthModal, which adapts its copy based on
          camera.status. */}
      {camera.status === "online" && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onAuthClick();
          }}
          title="Update this camera's sign-in"
          className="shrink-0 w-8 h-8 flex items-center justify-center rounded text-[#555] hover:text-[#aaa] hover:bg-[#2a2a2a] transition-colors"
        >
          <svg
            className="w-4 h-4"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M21 2l-9.5 9.5M15 2h6v6M11.5 11.5a4 4 0 1 1-4 4 8 8 0 0 0 4-4z"
            />
          </svg>
        </button>
      )}

      {/* Delete affordance. Two-click confirmation: first click arms
          the button (icon turns red, label appears), second click
          within 3 seconds actually deletes. Lets the user drop a
          permanently-offline or mis-detected camera without hunting
          through menus, and without the modal weight that a less
          destructive action would deserve. */}
      <button
        onClick={handleDeleteClick}
        disabled={deleting}
        title={deleteArmed ? "Click again to confirm" : "Remove this camera"}
        className={`shrink-0 w-8 h-8 flex items-center justify-center rounded transition-colors ${
          deleteArmed
            ? "bg-red-500/20 text-red-400 hover:bg-red-500/30"
            : "text-[#555] hover:text-[#aaa] hover:bg-[#2a2a2a]"
        } ${deleting ? "opacity-50 cursor-wait" : ""}`}
      >
        <svg
          className="w-4 h-4"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          viewBox="0 0 24 24"
        >
          <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6h14z" />
        </svg>
      </button>
    </div>
  );
}
