import { useEffect, useState } from "react";
import { Dashboard } from "./components/Dashboard";
import { DiscoveryScreen } from "./components/DiscoveryScreen";
import { OnboardingScreen } from "./components/OnboardingScreen";
import { Playback } from "./components/Playback";
import { ScanScreen } from "./components/ScanScreen";
import { useDiscovery } from "./hooks/useDiscovery";
import { apiFetch } from "./lib/backend";
import type { AppScreen } from "./types";

export default function App() {
  const { cameras, scanStatus, connected, activeMotion } = useDiscovery();
  // Start on a neutral placeholder — we need to know onboarding state
  // from the backend before we can pick the right initial screen.
  // ScanScreen is a safe placeholder because it already handles the
  // "backend not connected yet" state gracefully.
  const [screen, setScreen] = useState<AppScreen>("scan");
  const [playbackCameraId, setPlaybackCameraId] = useState<string | undefined>();
  const [playbackStartedAt, setPlaybackStartedAt] = useState<string | undefined>();
  const [hasAutoRouted, setHasAutoRouted] = useState(false);
  const [onboardingDone, setOnboardingDone] = useState<boolean | null>(null);

  // First-load onboarding check. Must happen before the cameras-based
  // auto-routing below, so we don't flash the dashboard before we
  // realize we should be showing the welcome flow.
  useEffect(() => {
    if (!connected) return;
    if (onboardingDone !== null) return;
    (async () => {
      try {
        const resp = await apiFetch("/api/settings");
        if (!resp.ok) {
          // Treat inability to read settings as "not done" — safer to
          // show the onboarding screen than to skip it.
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
  // - If we already have authenticated cameras, jump straight to Dashboard
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
      setScreen("dashboard");
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
            setScreen("scan");
          }}
        />
      )}
      {screen === "scan" && (
        <ScanScreen
          scanStatus={scanStatus}
          camerasFound={cameras.length}
          connected={connected}
          onContinue={() => setScreen("discovery")}
        />
      )}
      {screen === "discovery" && (
        <DiscoveryScreen
          cameras={cameras}
          onContinue={() => setScreen("dashboard")}
        />
      )}
      {screen === "dashboard" && (
        <Dashboard
          cameras={cameras}
          activeMotion={activeMotion}
          onPlayback={(camId, startedAt) => {
            setPlaybackCameraId(camId);
            setPlaybackStartedAt(startedAt);
            setScreen("playback");
          }}
          onManageCameras={() => setScreen("discovery")}
        />
      )}
      {screen === "playback" && (
        <Playback
          cameras={cameras}
          onBack={() => setScreen("dashboard")}
          initialCameraId={playbackCameraId}
          initialStartedAt={playbackStartedAt}
        />
      )}
    </>
  );
}
