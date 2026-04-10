import { useEffect, useState } from "react";
import { DiscoveryScreen } from "./components/DiscoveryScreen";
import { Home } from "./components/Home";
import { NameCamerasScreen } from "./components/NameCamerasScreen";
import { Recordings } from "./components/Recordings";
import { useDiscovery } from "./hooks/useDiscovery";
import { apiFetch } from "./lib/backend";
import type { AppScreen } from "./types";

export default function App() {
  const {
    cameras,
    scanStatus,
    connected,
    activeMotion,
    lastRecordingsDeleted,
    recentMotionEvents,
    go2rtcBaseUrl,
    modelDownload,
    storyEnabled,
  } = useDiscovery();
  // Start on the discovery screen directly — its "connecting" phase
  // is the initial placeholder while the backend readiness check
  // runs, and it transitions through scanning → found without a
  // screen change. Previously a separate ScanScreen owned the
  // connecting/scanning phases, which made the first-run flow three
  // screens instead of one.
  const [screen, setScreen] = useState<AppScreen>("discovery");
  const [playbackCameraId, setPlaybackCameraId] = useState<string | undefined>();
  const [playbackStartedAt, setPlaybackStartedAt] = useState<string | undefined>();
  const [hasAutoRouted, setHasAutoRouted] = useState(false);
  // When the user enters Discovery from somewhere other than the
  // first-run auto-route, remember where they came from so "Done"
  // returns there instead of forwarding to Home.
  const [discoveryReturnTo, setDiscoveryReturnTo] =
    useState<AppScreen | null>(null);
  // Same idea as discoveryReturnTo: Name cameras is launched from both
  // Home and (now) Recordings, and "Done" should return to whichever
  // screen launched it.
  const [nameCamerasReturnTo, setNameCamerasReturnTo] =
    useState<AppScreen>("home");

  // Silent onboarding migration. The brand-picker screen is gone —
  // it was a backend confidence hint that users routinely skipped,
  // contributing nothing to detection. To preserve the existing
  // onboarding_completed API contract (and avoid re-prompting users
  // post-upgrade), we quietly flip the flag to true once on first
  // backend contact if it's still false. Users never see a screen.
  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    (async () => {
      try {
        const resp = await apiFetch("/api/settings");
        if (!resp.ok || cancelled) return;
        const settings = await resp.json();
        if (settings.onboarding_completed) return;
        await apiFetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...settings, onboarding_completed: true }),
        });
      } catch {
        // Non-fatal — the zero-config migration is best-effort. Users
        // on a broken settings backend still see DiscoveryScreen.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [connected]);

  // First-load auto-routing:
  // - If we already have authenticated cameras, jump straight to Home
  //   (which shows live tiles + the history panel side-by-side)
  // - If we have discovered but unauth'd cameras, go to Discovery
  // - Otherwise stay on Discovery (it owns the connecting/scanning phases)
  useEffect(() => {
    if (hasAutoRouted) return;
    if (!connected) return;

    if (!scanStatus.last_scan && cameras.length === 0) return;

    const hasOnline = cameras.some((c) => c.status === "online" && c.rtsp_uri);
    if (hasOnline) {
      // Home is the hero landing view: split view with history on the
      // left and live camera tiles on the right. A brand-new user with
      // zero motion events still sees their cameras light up on the
      // right half, so the "empty inbox on first run" problem from the
      // old Inbox-as-hero design is structurally impossible here.
      setScreen("home");
      setHasAutoRouted(true);
    } else if (cameras.length > 0) {
      setScreen("discovery");
      setHasAutoRouted(true);
    }
  }, [connected, cameras, scanStatus.last_scan, hasAutoRouted]);

  return (
    <>
      {/* Model download banner */}
      {modelDownload && (
        <div
          className={`fixed top-0 left-0 right-0 z-50 px-4 py-2.5 text-center text-sm font-medium transition-colors ${
            modelDownload.status === "downloading"
              ? "bg-blue-600/90 text-white"
              : modelDownload.status === "done"
                ? "bg-emerald-600/90 text-white"
                : "bg-red-600/90 text-white"
          }`}
        >
          {modelDownload.status === "downloading" && (
            <span className="inline-block w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin mr-2 align-[-2px]" />
          )}
          {modelDownload.message}
          {modelDownload.status !== "downloading" && (
            <button
              onClick={() => {/* modelDownload is auto-dismissed */}}
              className="ml-3 opacity-70 hover:opacity-100"
            >
              &times;
            </button>
          )}
        </div>
      )}
      {screen === "discovery" && (
        <DiscoveryScreen
          cameras={cameras}
          connected={connected}
          scanStatus={scanStatus}
          // First-run (no return target set) auto-advances to Home a
          // grace period after the first camera comes online.
          // Revisits stay put until the user explicitly clicks Done.
          autoAdvance={discoveryReturnTo === null}
          onContinue={() => {
            const target = discoveryReturnTo ?? "home";
            setDiscoveryReturnTo(null);
            setScreen(target);
          }}
        />
      )}
      {screen === "home" && (
        <Home
          cameras={cameras}
          activeMotion={activeMotion}
          initialMotionEvents={recentMotionEvents}
          go2rtcBaseUrl={go2rtcBaseUrl}
          storyEnabled={storyEnabled}
          onBrowseFootage={(camId, startedAt) => {
            setPlaybackCameraId(camId);
            setPlaybackStartedAt(startedAt);
            setScreen("playback");
          }}
          onManageCameras={() => {
            setDiscoveryReturnTo("home");
            setScreen("discovery");
          }}
          onNameCameras={() => {
            setNameCamerasReturnTo("home");
            setScreen("name-cameras");
          }}
        />
      )}
      {screen === "name-cameras" && (
        <NameCamerasScreen
          cameras={cameras}
          onDone={() => setScreen(nameCamerasReturnTo)}
        />
      )}
      {screen === "playback" && (
        <Recordings
          cameras={cameras}
          onBack={() => setScreen("home")}
          onNameCameras={() => {
            setNameCamerasReturnTo("playback");
            setScreen("name-cameras");
          }}
          initialCameraId={playbackCameraId}
          initialStartedAt={playbackStartedAt}
          lastRecordingsDeleted={lastRecordingsDeleted}
          initialMotionEvents={recentMotionEvents}
          storyEnabled={storyEnabled}
        />
      )}
    </>
  );
}
