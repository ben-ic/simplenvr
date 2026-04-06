import { useEffect, useState } from "react";
import { Dashboard } from "./components/Dashboard";
import { DiscoveryScreen } from "./components/DiscoveryScreen";
import { Playback } from "./components/Playback";
import { ScanScreen } from "./components/ScanScreen";
import { useDiscovery } from "./hooks/useDiscovery";
import type { AppScreen } from "./types";

export default function App() {
  const { cameras, scanStatus, connected, activeMotion } = useDiscovery();
  const [screen, setScreen] = useState<AppScreen>("scan");
  const [playbackCameraId, setPlaybackCameraId] = useState<string | undefined>();
  const [playbackStartedAt, setPlaybackStartedAt] = useState<string | undefined>();
  const [hasAutoRouted, setHasAutoRouted] = useState(false);

  // First-load auto-routing:
  // - If we already have authenticated cameras, jump straight to Dashboard
  // - If we have discovered but unauth'd cameras, go to Discovery
  // - Otherwise stay on Scan
  useEffect(() => {
    if (hasAutoRouted) return;
    if (!connected) return;
    if (!scanStatus.last_scan && cameras.length === 0) return;

    const hasOnline = cameras.some((c) => c.status === "online" && c.rtsp_uri);
    if (hasOnline) {
      setScreen("dashboard");
      setHasAutoRouted(true);
    } else if (cameras.length > 0) {
      setScreen("discovery");
      setHasAutoRouted(true);
    }
  }, [connected, cameras, scanStatus.last_scan, hasAutoRouted]);

  return (
    <>
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
