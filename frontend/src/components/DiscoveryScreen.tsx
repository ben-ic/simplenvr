import { useEffect, useMemo, useRef, useState } from "react";
import {
  deleteCamera,
  triggerScan,
  updateCameraName,
} from "../api/client";
import type { Camera, ScanStatus } from "../types";
import { AuthModal } from "./AuthModal";
import { ManualAddCameraModal } from "./ManualAddCameraModal";

// ---------------------------------------------------------------------------
// DiscoveryScreen — the first-run camera setup flow in the editorial
// "First Edition" direction. The whole onboarding (this screen plus
// SetupScreen) uses the same masthead, the same typography, and the
// same voice so the user doesn't feel a visual jolt between phases.
//
// State machine (driven by props, no local transitions):
//   connecting : backend not reachable yet             → "Starting up."
//   scanning   : scan in progress, zero cameras yet    → "Looking around."
//   empty      : scan done, zero cameras               → "No cameras yet."
//   found      : at least one camera discovered        → numbered list
//
// The found phase uses a numbered list-as-typography layout rather than
// a table. Thumbnails are deliberately absent here — they live on
// SetupScreen, where the user is naming cameras and needs to see which
// feed corresponds to which name. At the sign-in step, the user cares
// about brand, IP, and status; visual noise is a distraction.
// ---------------------------------------------------------------------------

const CHIP_BANKS: string[][] = [
  ["Driveway", "Front door", "Garage", "Side gate"],
  ["Back yard", "Patio", "Pool", "Deck"],
  ["Front porch", "Walkway", "Entry", "Lobby"],
  ["Parking", "Back door", "Loading dock", "Side alley"],
  ["Living room", "Office", "Hallway", "Nursery"],
  ["Shop floor", "Register", "Aisle 1", "Storeroom"],
];

interface DiscoveryScreenProps {
  cameras: Camera[];
  connected: boolean;
  scanStatus: ScanStatus;
  onContinue: () => void;
}

export function DiscoveryScreen({
  cameras,
  connected,
  scanStatus,
  onContinue,
}: DiscoveryScreenProps) {
  const [authCamera, setAuthCamera] = useState<Camera | null>(null);
  const [rescanning, setRescanning] = useState(false);
  const [showManualAdd, setShowManualAdd] = useState(false);
  // IDs of cameras the backend is trying to sign in as a sibling of a
  // just-authenticated same-manufacturer camera. The backend sequentially
  // probes each one; without this optimistic pill the user sees stale
  // "Sign in" buttons while the cascade grinds through.
  const [signingInIds, setSigningInIds] = useState<Set<string>>(new Set());
  // Transient ack after a manual-add. Auto-dismisses after 5s.
  const [justAdded, setJustAdded] = useState<{ id: string; label: string } | null>(
    null,
  );

  const onlineCount = cameras.filter((c) => c.status === "online").length;
  const scanComplete = scanStatus.last_scan !== null;
  const isScanning = scanStatus.scanning || rescanning;

  // Grace period before we surface the "No cameras yet" empty state.
  // The backend scanner runs every 30 seconds automatically, so the
  // first scan finishing with 0 cameras isn't actually a "give up"
  // moment — there's another scan coming in 30s. Jumping to the empty
  // screen the instant scanComplete flips to true feels like the app
  // has given up and puts the "Search again" button in front of the
  // user when it should be keeping them in the "looking" state a bit
  // longer. We give the scanner two full cycles (~60s) to discover
  // cameras that were slow to respond to the first pass before we
  // resign to the empty state.
  const EMPTY_STATE_GRACE_MS = 60_000;
  const [showEmptyAllowed, setShowEmptyAllowed] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setShowEmptyAllowed(true), EMPTY_STATE_GRACE_MS);
    return () => clearTimeout(t);
  }, []);

  const phase: "connecting" | "scanning" | "empty" | "found" = !connected
    ? "connecting"
    : cameras.length > 0
      ? "found"
      : cameras.length === 0 && scanComplete && showEmptyAllowed
        ? "empty"
        : "scanning";

  // Clear optimistic "signing in" state once the cascade catches up.
  useEffect(() => {
    if (signingInIds.size === 0) return;
    const stillRelevant = new Set<string>();
    for (const id of signingInIds) {
      const cam = cameras.find((c) => c.id === id);
      if (cam && cam.status === "needs_auth") stillRelevant.add(id);
    }
    if (stillRelevant.size !== signingInIds.size) {
      setSigningInIds(stillRelevant);
      return;
    }
    const timeout = setTimeout(() => setSigningInIds(new Set()), 25_000);
    return () => clearTimeout(timeout);
  }, [cameras, signingInIds]);

  // Auto-dismiss the "Added X" ack banner after 5s.
  useEffect(() => {
    if (!justAdded) return;
    const t = setTimeout(() => setJustAdded(null), 5_000);
    return () => clearTimeout(t);
  }, [justAdded]);

  const handleAuthSuccess = (
    _username: string,
    _password: string,
    applyAll: boolean,
  ) => {
    if (!applyAll || !authCamera?.manufacturer) return;
    const siblings = cameras
      .filter(
        (c) =>
          c.id !== authCamera.id &&
          c.status === "needs_auth" &&
          c.manufacturer === authCamera.manufacturer,
      )
      .map((c) => c.id);
    if (siblings.length === 0) return;
    setSigningInIds((prev) => {
      const next = new Set(prev);
      for (const id of siblings) next.add(id);
      return next;
    });
  };

  const handleManualAdded = (camera: Camera) => {
    setJustAdded({ id: camera.id, label: camera.name || camera.ip });
    handleRescan();
  };

  const handleRescan = async () => {
    setRescanning(true);
    try {
      await triggerScan();
    } finally {
      setRescanning(false);
    }
  };

  // Masthead date. Generated once; the setup session typically takes
  // less than 10 minutes so we never cross midnight.
  const dateLabel = useMemo(() => {
    try {
      return new Date().toLocaleDateString(undefined, {
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric",
      });
    } catch {
      return new Date().toDateString();
    }
  }, []);

  const titleText = (() => {
    switch (phase) {
      case "connecting":
        return "Starting up.";
      case "scanning":
        return "Looking around.";
      case "empty":
        return "No cameras yet.";
      case "found":
        return cameras.length === 1
          ? "One camera found."
          : `${cameras.length} cameras found.`;
    }
  })();

  return (
    <div className="flex-1 flex flex-col bg-[#0a0a0a] text-[#ededed] overflow-y-auto">
      {/* Topbar — same chrome across the whole onboarding */}
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

        {/* Hero title */}
        <h1 className="text-[52px] font-bold tracking-[-0.035em] leading-[0.97] text-[#ededed] mb-[22px]">
          {titleText}
        </h1>

        {/* Phase-specific body */}
        {phase === "connecting" && <ConnectingBody />}
        {phase === "scanning" && <ScanningBody foundSoFar={cameras.length} />}
        {phase === "empty" && (
          <EmptyBody
            onRescan={handleRescan}
            onManualAdd={() => setShowManualAdd(true)}
            onSkip={onContinue}
            isScanning={isScanning}
          />
        )}
        {phase === "found" && (
          <FoundBody
            cameras={cameras}
            signingInIds={signingInIds}
            justAdded={justAdded}
            onlineCount={onlineCount}
            isScanning={isScanning}
            onAuthClick={(cam) => setAuthCamera(cam)}
            onManualAdd={() => setShowManualAdd(true)}
            onRescan={handleRescan}
            onContinue={onContinue}
          />
        )}
      </div>

      {/* Modals */}
      {authCamera && (
        <AuthModal
          camera={authCamera}
          onClose={() => setAuthCamera(null)}
          onSuccess={handleAuthSuccess}
        />
      )}
      {showManualAdd && (
        <ManualAddCameraModal
          onClose={() => setShowManualAdd(false)}
          onAdded={handleManualAdded}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Connecting + Scanning bodies — both minimal, just lede + hairline progress.
// ---------------------------------------------------------------------------
function ConnectingBody() {
  return (
    <>
      <p className="text-[15px] leading-[1.7] text-[#a0a0a0] max-w-[540px] mb-14">
        Just a moment while we wake everything up. This only happens on the
        very first launch &mdash; next time you open SimpleNVR, it&rsquo;ll be
        ready right away.
      </p>
      <ProgressRail progress="low" />
      <ProgressCaption>
        Waking up <span className="text-[#2a2a2a]">·</span>{" "}
        almost ready
      </ProgressCaption>
    </>
  );
}

function ScanningBody({ foundSoFar }: { foundSoFar: number }) {
  return (
    <>
      <p className="text-[15px] leading-[1.7] text-[#a0a0a0] max-w-[540px] mb-14">
        We&rsquo;re looking for your cameras. If yours are plugged in and
        switched on, they should appear here in a moment.
      </p>
      <ProgressRail progress="mid" />
      <ProgressCaption>
        Looking around <span className="text-[#2a2a2a]">·</span>{" "}
        <span className="tabular-nums">{foundSoFar}</span>{" "}
        {foundSoFar === 1 ? "camera" : "cameras"} so far
      </ProgressCaption>
    </>
  );
}

// ---------------------------------------------------------------------------
// Empty body — nothing found. Give the user three clear things to check
// and two ways forward.
// ---------------------------------------------------------------------------
function EmptyBody({
  onRescan,
  onManualAdd,
  onSkip,
  isScanning,
}: {
  onRescan: () => void;
  onManualAdd: () => void;
  onSkip: () => void;
  isScanning: boolean;
}) {
  return (
    <>
      <p className="text-[15px] leading-[1.7] text-[#a0a0a0] max-w-[540px] mb-10">
        We looked and didn&rsquo;t find any cameras yet. A couple of things
        worth a quick check, then we can try again:
      </p>

      <ul className="border-t border-[#1a1a1a] mb-12">
        <EmptyHint
          num="01"
          text="Each camera is plugged in and switched on. If the camera has a little light on it, that light should be on."
        />
        <EmptyHint
          num="02"
          text="Each camera is using the same Wi-Fi or router as this computer. Cameras on a different Wi-Fi can't be found automatically."
        />
        <EmptyHint
          num="03"
          text="Already know your camera's IP address? You can add it using the button below."
        />
      </ul>

      <div className="flex items-center gap-3 mb-8">
        <button
          type="button"
          onClick={onRescan}
          disabled={isScanning}
          className="bg-blue-500 hover:bg-blue-400 text-white px-6 py-3 rounded-[3px] text-[11px] font-bold uppercase tracking-[0.14em] transition-colors disabled:opacity-50 disabled:cursor-wait border-none cursor-pointer"
        >
          {isScanning ? "Searching\u2026" : "Search again"}
        </button>
        <button
          type="button"
          onClick={onManualAdd}
          className="bg-transparent border border-[#2a2a2a] text-[#a0a0a0] hover:text-[#ededed] hover:border-[#333] px-6 py-3 rounded-[3px] text-[11px] font-bold uppercase tracking-[0.14em] transition-colors cursor-pointer"
        >
          Add by IP address
        </button>
      </div>

      <button
        type="button"
        onClick={onSkip}
        className="text-[11px] font-bold uppercase tracking-[0.14em] text-[#444] hover:text-[#666] bg-transparent border-none p-0 cursor-pointer transition-colors"
      >
        &larr; Skip for now
      </button>
    </>
  );
}

function EmptyHint({ num, text }: { num: string; text: string }) {
  return (
    <li className="grid grid-cols-[48px_1fr] gap-4 py-4 border-b border-[#1a1a1a]">
      <span className="text-[11px] font-bold uppercase tracking-[0.18em] text-[#444] tabular-nums pt-0.5">
        {num}
      </span>
      <span className="text-[14px] leading-[1.65] text-[#a0a0a0]">{text}</span>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Found body — one or more cameras discovered. Numbered list with
// inline rename + status on the right.
// ---------------------------------------------------------------------------
function FoundBody({
  cameras,
  signingInIds,
  justAdded,
  onlineCount,
  isScanning,
  onAuthClick,
  onManualAdd,
  onRescan,
  onContinue,
}: {
  cameras: Camera[];
  signingInIds: Set<string>;
  justAdded: { id: string; label: string } | null;
  onlineCount: number;
  isScanning: boolean;
  onAuthClick: (cam: Camera) => void;
  onManualAdd: () => void;
  onRescan: () => void;
  onContinue: () => void;
}) {
  const needsAuthCount = cameras.filter((c) => c.status === "needs_auth").length;
  const hasSignedInAnyone = onlineCount > 0;

  const subText = (() => {
    if (cameras.length === 1) {
      if (onlineCount === 1) {
        return "Your camera is signed in. Click Continue when you\u2019re ready to set it up.";
      }
      return "Sign in with the username and password from the camera\u2019s own app.";
    }
    if (onlineCount === 0) {
      return "Sign in to each one to begin recording. Most cameras use admin as the username and whatever password you set in the camera\u2019s own app.";
    }
    if (needsAuthCount === 0) {
      return "All signed in. Click Continue when you\u2019re ready to set them up.";
    }
    return `${onlineCount} signed in so far, ${needsAuthCount} to go. Keep going, then click Continue.`;
  })();

  return (
    <>
      <p className="text-[15px] leading-[1.7] text-[#a0a0a0] max-w-[580px] mb-14">
        {subText}
      </p>

      {/* Numbered list of cameras */}
      <div className="mb-12">
        <div className="grid grid-cols-[auto_auto_1fr_auto] items-center gap-4 mb-[18px]">
          <span className="text-[11px] font-bold uppercase tracking-[0.2em] text-[#666] tabular-nums">
            01
          </span>
          <span className="text-[19px] font-bold tracking-[-0.015em] text-[#ededed]">
            Your cameras
          </span>
          <span className="h-px bg-[#1a1a1a]" />
          <span className="text-[11px] font-medium text-[#666] tabular-nums">
            {onlineCount} of {cameras.length} signed in
          </span>
        </div>

        {justAdded && (
          <div className="mb-3 px-4 py-2.5 bg-blue-500/8 border border-blue-500/25 rounded-[3px] text-[12px] text-blue-300 flex items-center gap-2">
            <svg
              className="w-3 h-3 shrink-0"
              fill="none"
              stroke="currentColor"
              strokeWidth={2.5}
              viewBox="0 0 24 24"
            >
              <path d="M20 6 9 17l-5-5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span>
              Added <span className="font-mono text-[#ededed]">{justAdded.label}</span>. Checking the connection.
            </span>
          </div>
        )}

        <ul className="border-t border-[#1a1a1a]">
          {cameras.map((cam, idx) => (
            <CameraListItem
              key={cam.id}
              camera={cam}
              number={String(idx + 1).padStart(2, "0")}
              chips={CHIP_BANKS[idx % CHIP_BANKS.length]}
              signingIn={signingInIds.has(cam.id)}
              onAuthClick={() => onAuthClick(cam)}
            />
          ))}
        </ul>
      </div>

      {/* Footer actions */}
      <div className="flex items-start justify-between gap-5 pt-8 border-t border-[#1a1a1a]">
        <div className="flex flex-col gap-1 max-w-[360px]">
          <button
            type="button"
            onClick={onManualAdd}
            className="text-left text-[11px] font-bold uppercase tracking-[0.14em] text-[#666] hover:text-[#ededed] transition-colors bg-transparent border-none p-0 cursor-pointer"
          >
            + Add by IP address
          </button>
          <button
            type="button"
            onClick={onRescan}
            disabled={isScanning}
            className="text-left text-[11px] font-bold uppercase tracking-[0.14em] text-[#666] hover:text-[#ededed] transition-colors bg-transparent border-none p-0 cursor-pointer disabled:opacity-50 disabled:cursor-wait mt-1"
          >
            {isScanning ? "Searching\u2026" : "\u21bb Search again"}
          </button>
        </div>
        <button
          type="button"
          onClick={onContinue}
          disabled={!hasSignedInAnyone}
          className="bg-blue-500 hover:bg-blue-400 text-white px-6 py-3 rounded-[3px] text-[11px] font-bold uppercase tracking-[0.14em] disabled:opacity-30 disabled:cursor-not-allowed transition-colors flex items-center gap-3 border-none cursor-pointer"
        >
          Continue
          <span aria-hidden="true">&rarr;</span>
        </button>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// CameraListItem — one row in the numbered list. Owns its own rename
// + delete state, reads camera props directly (no sync-state-from-prop).
// ---------------------------------------------------------------------------
function CameraListItem({
  camera,
  number,
  chips,
  signingIn,
  onAuthClick,
}: {
  camera: Camera;
  number: string;
  chips: string[];
  signingIn: boolean;
  onAuthClick: () => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const editing = draft !== null;
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const startEditing = (e: React.MouseEvent) => {
    e.stopPropagation();
    setDraft(camera.name ?? "");
    setTimeout(() => inputRef.current?.select(), 0);
  };

  const commitEdit = async () => {
    const trimmed = (draft ?? "").trim();
    const previous = camera.name ?? "";
    setDraft(null);
    if (!trimmed || trimmed === previous) return;
    try {
      await updateCameraName(camera.id, trimmed);
    } catch {
      // Silent revert — the next ws snapshot will reconcile
    }
  };

  const cancelEdit = () => setDraft(null);

  const applyChip = (chip: string) => {
    setDraft(chip);
    setTimeout(() => inputRef.current?.focus(), 0);
  };

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
    } catch {
      setDeleting(false);
      setDeleteArmed(false);
    }
  };

  const displayName = (() => {
    if (camera.name) return camera.name;
    const parts = [camera.manufacturer, camera.model].filter(Boolean);
    if (parts.length > 0) return parts.join(" ");
    return "Unknown camera";
  })();

  const subtitleParts: string[] = [];
  if (camera.name && camera.manufacturer) {
    subtitleParts.push(
      [camera.manufacturer, camera.model].filter(Boolean).join(" "),
    );
  }
  if (camera.resolutions[0]) subtitleParts.push(camera.resolutions[0]);
  subtitleParts.push(camera.ip);
  if (
    camera.hostname &&
    !/^(unknown|localhost|\s*)$/i.test(camera.hostname)
  ) {
    subtitleParts.push(camera.hostname);
  }

  return (
    <li className="grid grid-cols-[40px_1fr_auto] gap-5 items-start py-5 border-b border-[#1a1a1a] group">
      {/* Number */}
      <span className="text-[11px] font-bold uppercase tracking-[0.18em] text-[#444] tabular-nums pt-1.5">
        {number}
      </span>

      {/* Name + caption */}
      <div className="min-w-0">
        {editing ? (
          <>
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
              className="w-full max-w-[360px] bg-transparent border-0 border-b border-blue-500 pb-0.5 text-[18px] font-bold tracking-[-0.012em] text-[#ededed] outline-none caret-blue-500 placeholder:text-[#444] placeholder:italic placeholder:font-medium"
              style={{ borderBottomWidth: "1.5px" }}
            />
            <div className="flex flex-wrap gap-x-3.5 gap-y-1.5 mt-2">
              {chips.map((chip) => (
                <button
                  key={chip}
                  type="button"
                  onMouseDown={(e) => {
                    e.preventDefault();
                    applyChip(chip);
                  }}
                  className="bg-transparent border-0 p-0 text-[11px] font-medium text-[#666] hover:text-blue-400 transition-colors cursor-pointer"
                >
                  {chip}
                </button>
              ))}
            </div>
          </>
        ) : (
          <>
            <button
              type="button"
              onClick={startEditing}
              className="text-left text-[18px] tracking-[-0.012em] leading-[1.2] mb-1 bg-transparent border-none p-0 cursor-text transition-colors font-bold text-[#ededed] hover:text-blue-400"
              title="Click to rename"
            >
              {displayName}
              {camera.identification_source === "fingerprint" &&
                !camera.model && (
                  <span className="ml-2 text-[10px] font-medium uppercase tracking-[0.1em] text-[#444]">
                    best guess
                  </span>
                )}
            </button>
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-[#666]">
              {subtitleParts.map((part, i) => (
                <span key={i} className="flex items-center gap-2.5">
                  {i > 0 && <span className="w-2.5 h-px bg-[#2a2a2a]" />}
                  <span>{part}</span>
                </span>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Status column */}
      <div className="flex items-center gap-2 pt-1.5 justify-end">
        {signingIn ? (
          <span className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.16em] text-blue-400">
            <Spinner />
            Signing in
          </span>
        ) : camera.status === "online" ? (
          <span className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.16em] text-[#4ade80]">
            <span className="w-1.5 h-1.5 rounded-full bg-[#4ade80]" />
            Online
          </span>
        ) : camera.status === "needs_auth" ? (
          <button
            type="button"
            onClick={onAuthClick}
            className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.16em] text-[#fbbf24] bg-amber-500/8 border border-amber-500/30 hover:border-amber-500/60 hover:bg-amber-500/12 px-3 py-1.5 rounded-[3px] transition-colors cursor-pointer"
          >
            Sign in
            <span aria-hidden="true">&rarr;</span>
          </button>
        ) : camera.status === "asleep" ? (
          <span className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.16em] text-[#666]">
            Asleep
          </span>
        ) : (
          <span className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.16em] text-[#666]">
            Offline
          </span>
        )}

        <button
          type="button"
          onClick={handleDeleteClick}
          disabled={deleting}
          title={
            deleteArmed ? "Click again to confirm" : "Remove this camera"
          }
          className={`w-6 h-6 flex items-center justify-center rounded-sm opacity-0 group-hover:opacity-100 focus:opacity-100 transition-all bg-transparent border-none cursor-pointer ${
            deleteArmed
              ? "text-red-400 hover:text-red-300 opacity-100"
              : "text-[#444] hover:text-[#888]"
          } ${deleting ? "cursor-wait opacity-50" : ""}`}
        >
          <svg
            className="w-3.5 h-3.5"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            viewBox="0 0 24 24"
          >
            <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round" />
          </svg>
        </button>
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function ProgressRail({ progress }: { progress: "low" | "mid" }) {
  // Hairline animated track. The "flow-rail" keyframe is injected
  // inline so we don't need a global keyframes block.
  return (
    <>
      <style>{`
        @keyframes flow-rail {
          0%   { transform: translateX(-100%); }
          100% { transform: translateX(200%); }
        }
      `}</style>
      <div className="relative h-0.5 bg-[#1a1a1a] max-w-[420px] overflow-hidden mb-5">
        <div
          className="absolute top-0 left-0 h-full bg-blue-500"
          style={{
            width: progress === "low" ? "30%" : "55%",
            animation: `flow-rail ${progress === "low" ? "2.4s" : "2.8s"} cubic-bezier(0.16, 1, 0.3, 1) infinite`,
          }}
        />
      </div>
    </>
  );
}

function ProgressCaption({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[11px] font-medium text-[#666] flex items-center gap-2">
      <span className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse" />
      {children}
    </div>
  );
}

function Spinner() {
  return (
    <svg
      className="w-3 h-3 animate-spin"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.5}
      viewBox="0 0 24 24"
    >
      <path d="M21 12a9 9 0 1 1-6.22-8.56" strokeLinecap="round" />
    </svg>
  );
}
