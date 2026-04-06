import { useState } from "react";
import { Dashboard } from "./components/Dashboard";
import { DiscoveryScreen } from "./components/DiscoveryScreen";
import { Playback } from "./components/Playback";
import { ScanScreen } from "./components/ScanScreen";
import { useDiscovery } from "./hooks/useDiscovery";
import type { AppScreen } from "./types";

export default function App() {
  const { cameras, scanStatus, connected } = useDiscovery();
  const [screen, setScreen] = useState<AppScreen>("scan");
  const [playbackCameraId, setPlaybackCameraId] = useState<string | undefined>();

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
          onPlayback={(camId) => {
            setPlaybackCameraId(camId);
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
        />
      )}
    </>
  );
}
