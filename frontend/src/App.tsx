import { useEffect, useMemo, useState } from "react";
import { setAllVisible } from "tauri-plugin-rtsp-mosaic-api";
import { DiscoveryScreen } from "./components/DiscoveryScreen";
import { Home } from "./components/Home";
import { Recordings } from "./components/Recordings";
import { SetupScreen } from "./components/SetupScreen";
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

  // Navigation model:
  //
  // The screen the user sees is derived. We compute `autoScreen` from the
  // current conditions (connected, cameras, onboarding flag) and use that
  // by default. As soon as the user takes any navigation action, we set
  // `userScreen` and that wins forever (or until they take another
  // action). This avoids the setState-inside-effect anti-pattern: the
  // screen flips automatically as conditions resolve, no imperative
  // setScreen calls sitting inside an effect body.
  const [userScreen, setUserScreen] = useState<AppScreen | null>(null);
  const [playbackCameraId, setPlaybackCameraId] = useState<string | undefined>();
  const [playbackStartedAt, setPlaybackStartedAt] = useState<string | undefined>();
  // Onboarding gate: null = still loading, true/false once we've read it.
  const [onboardingCompleted, setOnboardingCompleted] =
    useState<boolean | null>(null);

  // Fetch the onboarding flag on first backend contact. Unlike the old
  // "silent migration" effect, we do NOT auto-flip the flag here — it's
  // set explicitly by SetupScreen when the user hits Begin recording.
  //
  // The settings endpoint returns 503 {"status": "starting"} until the
  // recorder is loaded (5-10s on first launch while classifier + YAMNet
  // models warm up), so a one-shot fetch races the backend and loses.
  // Poll every 500 ms until a 200 lands or the effect is torn down.
  useEffect(() => {
    if (!connected) return;
    if (onboardingCompleted !== null) return;
    let cancelled = false;

    const poll = async () => {
      while (!cancelled) {
        try {
          const resp = await apiFetch("/api/settings");
          if (cancelled) return;
          if (resp.ok) {
            const settings = await resp.json();
            if (!cancelled) {
              setOnboardingCompleted(Boolean(settings.onboarding_completed));
            }
            return;
          }
          // 503 while the recorder warms up — wait and retry.
        } catch {
          // Transient connectivity — retry.
        }
        await new Promise((r) => setTimeout(r, 500));
      }
    };

    void poll();
    return () => {
      cancelled = true;
    };
  }, [connected, onboardingCompleted]);

  // Auto-routing as a pure derivation. No setState inside an effect —
  // the screen just changes when the inputs change.
  //
  // First-time users (onboarding_completed === false) NEVER auto-route.
  // They stay on DiscoveryScreen until they click "Continue to live
  // view" themselves. If the auto-router yanked them the moment the
  // first of N cameras came online, they'd lose the chance to sign in
  // to the rest. DiscoveryScreen already has a visible Continue button
  // that appears once at least one camera is authed; let that drive
  // the transition.
  //
  // Return users (onboarding_completed === true) DO auto-route: they
  // already saw setup once, and a relaunch should land them on Home
  // with their existing cameras instead of making them click through
  // Discovery again.
  const autoScreen = useMemo<AppScreen>(() => {
    if (!connected) return "discovery";
    if (onboardingCompleted === null) return "discovery";
    // First-time users: always discovery. Manual Continue drives the rest.
    if (!onboardingCompleted) return "discovery";

    if (!scanStatus.last_scan && cameras.length === 0) return "discovery";

    const hasOnline = cameras.some(
      (c) => c.status === "online" && c.rtsp_uri,
    );
    if (hasOnline) return "home";
    return "discovery";
  }, [connected, cameras, scanStatus.last_scan, onboardingCompleted]);

  const screen: AppScreen = userScreen ?? autoScreen;

  // Hide/show native tiles based on whether Home is the active screen.
  // Home stays mounted so tiles keep their RTSP connections and overlay
  // state alive. We just toggle native NSView visibility so they don't
  // render over other screens.
  const homeActive = screen === "home";
  useEffect(() => {
    if (homeActive) {
      setAllVisible(true).catch(() => {});
    } else {
      setAllVisible(false).catch(() => {});
    }
  }, [homeActive]);

  // ── Navigation handlers ──
  // Each handler sets userScreen, which overrides the derived autoScreen
  // from here on. Subsequent navigation flows through these too.

  const handleDiscoveryContinue = () => {
    // Pessimistic default: if the flag is still loading (null), treat
    // the user as first-time and take them through setup. Better to
    // show setup to a user who already onboarded than to silently skip
    // it for a fresh install where the settings fetch hadn't resolved
    // yet when they clicked Continue.
    setUserScreen(onboardingCompleted === true ? "home" : "setup");
  };

  const handleSetupDone = () => {
    // SetupScreen already POST'd onboarding_completed: true. Reflect it
    // locally so the next auto-routing evaluation sees "onboarded".
    setOnboardingCompleted(true);
    setUserScreen("home");
  };

  const handleSetupBack = () => {
    setUserScreen("discovery");
  };

  const handleManageCameras = () => {
    setUserScreen("discovery");
  };

  const handleBrowseFootage = (camId?: string, startedAt?: string) => {
    setPlaybackCameraId(camId);
    setPlaybackStartedAt(startedAt);
    setUserScreen("playback");
  };

  const handlePlaybackBack = () => {
    setUserScreen("home");
  };

  return (
    <>
      {screen === "discovery" && (
        <DiscoveryScreen
          cameras={cameras}
          connected={connected}
          scanStatus={scanStatus}
          onContinue={handleDiscoveryContinue}
        />
      )}
      {screen === "setup" && (
        <SetupScreen
          cameras={cameras.filter(
            (c) => c.status === "online" && c.rtsp_uri,
          )}
          onDone={handleSetupDone}
          onBack={handleSetupBack}
        />
      )}
      {/* Home stays mounted so native tiles keep their RTSP connections
          and overlay state alive across screen transitions. We use
          visibility:hidden + position:absolute (not display:none) so the
          tiles keep their layout dimensions — display:none zeroes out
          getBoundingClientRect and breaks the custom element. */}
      <div
        className={screen === "home" ? "" : "invisible absolute inset-0 pointer-events-none"}
      >
        <Home
          cameras={cameras}
          activeMotion={activeMotion}
          initialMotionEvents={recentMotionEvents}
          onBrowseFootage={handleBrowseFootage}
          onManageCameras={handleManageCameras}
        />
      </div>
      {screen === "playback" && (
        <Recordings
          cameras={cameras}
          onBack={handlePlaybackBack}
          initialCameraId={playbackCameraId}
          initialStartedAt={playbackStartedAt}
          lastRecordingsDeleted={lastRecordingsDeleted}
          initialMotionEvents={recentMotionEvents}
        />
      )}
    </>
  );
}
