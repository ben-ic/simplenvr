import { useEffect, useState } from "react";
import { DiscoveryScreen } from "./components/DiscoveryScreen";
import { Home } from "./components/Home";
import { NameCamerasScreen } from "./components/NameCamerasScreen";
import { OnboardingScreen } from "./components/OnboardingScreen";
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
  const [onboardingDone, setOnboardingDone] = useState<boolean | null>(null);
  // When the user enters Discovery from somewhere other than the
  // first-run auto-route, remember where they came from so "Done"
  // returns there instead of forwarding to Home.
  const [discoveryReturnTo, setDiscoveryReturnTo] =
    useState<AppScreen | null>(null);

  // First-load onboarding check. Must happen before the cameras-based
  // auto-routing below, so we don't flash Home before we realize we
  // should be showing the welcome flow.
  useEffect(() => {
    if (!connected) return;
    if (onboardingDone !== null) return;
    (async () => {
      try {
        const resp = await apiFetch("/api/settings");
        if (!resp.ok) {
          setOnboardingDone(false);
          return;
        }
        const settings = await resp.json();
        setOnboardingDone(!!settings.onboarding_completed);
      } catch {
        setOnboardingDone(false);
      }
    })();
  }, [connected, onboardingDone]);

  // First-load auto-routing:
  // - If onboarding hasn't been completed, show OnboardingScreen
  // - If we already have authenticated cameras, jump straight to Home
  //   (which shows live tiles + the history panel side-by-side)
  // - If we have discovered but unauth'd cameras, go to Discovery
  // - Otherwise stay on Scan
  useEffect(() => {
    if (hasAutoRouted) return;
    if (!connected) return;
    if (onboardingDone === null) return;

    if (!onboardingDone) {
      setScreen("onboarding");
      setHasAutoRouted(true);
      return;
    }

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
  }, [connected, cameras, scanStatus.last_scan, hasAutoRouted, onboardingDone]);

  return (
    <>
      {screen === "onboarding" && (
        <OnboardingScreen
          onContinue={() => {
            setOnboardingDone(true);
            setScreen("discovery");
          }}
        />
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
          onBrowseFootage={(camId, startedAt) => {
            setPlaybackCameraId(camId);
            setPlaybackStartedAt(startedAt);
            setScreen("playback");
          }}
          onManageCameras={() => {
            setDiscoveryReturnTo("home");
            setScreen("discovery");
          }}
          onNameCameras={() => setScreen("name-cameras")}
        />
      )}
      {screen === "name-cameras" && (
        <NameCamerasScreen
          cameras={cameras}
          onDone={() => setScreen("home")}
        />
      )}
      {screen === "playback" && (
        <Recordings
          cameras={cameras}
          onBack={() => setScreen("home")}
          initialCameraId={playbackCameraId}
          initialStartedAt={playbackStartedAt}
          lastRecordingsDeleted={lastRecordingsDeleted}
          initialMotionEvents={recentMotionEvents}
        />
      )}
    </>
  );
}
