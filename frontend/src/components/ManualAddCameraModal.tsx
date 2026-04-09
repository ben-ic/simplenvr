import { useEffect, useRef, useState } from "react";
import { addCameraManually } from "../api/client";

/**
 * Escape-hatch modal for cameras that didn't auto-discover.
 *
 * The vast majority of users never open this — auto-discovery finds
 * their cameras, and they sign in from the main discovery screen. But
 * for cameras on a VLAN, cameras with ONVIF disabled, cameras on a
 * routed subnet, or power users who already know the URL, manual add
 * is the only way. The modal is intentionally compact and sensibly
 * defaulted (port 554, optional path, optional brand hint) so a user
 * who just types IP + username + password and clicks Add gets a
 * working camera when possible.
 *
 * The backend tries the user-supplied RTSP path first (if any), then
 * the brand's known path patterns (if a brand is selected), then a
 * small set of universal fallbacks. The first path that produces a
 * valid video stream wins.
 */

// Brands that we have working RTSP path patterns for. Pulled from
// backend/discovery/fingerprints.py. When the user picks one, the
// backend uses that brand's known paths as probe candidates.
const BRANDS_WITH_PATHS = [
  "Reolink",
  "Eufy",
  "TP-Link Tapo",
  "Hikvision",
  "Dahua",
  "Axis Communications",
  "Ubiquiti UniFi Protect",
  "Amcrest",
  "Uniview (UNV)",
  "Lorex",
  "Swann",
  "Foscam",
  "Annke",
  "Bosch Security",
  "Hanwha Wisenet",
  "Pelco",
  "Avigilon",
  "Vivotek",
  "Mobotix",
  "i-PRO (Panasonic)",
  "GeoVision",
  "Ezviz",
];

export function ManualAddCameraModal({
  onClose,
  onAdded,
}: {
  onClose: () => void;
  onAdded: () => void;
}) {
  const [ip, setIp] = useState("");
  const [port, setPort] = useState("554");
  const [brand, setBrand] = useState("");
  const [path, setPath] = useState("");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ipRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    ipRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const canSubmit = ip.trim().length > 0 && password.length > 0 && !loading;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setLoading(true);
    setError(null);
    try {
      const portNum = parseInt(port, 10);
      await addCameraManually({
        ip: ip.trim(),
        port: Number.isFinite(portNum) && portNum > 0 ? portNum : 554,
        path: path.trim() || undefined,
        brand: brand || undefined,
        username: username.trim() || "admin",
        password,
        name: name.trim() || undefined,
      });
      onAdded();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setLoading(false);
    }
  };

  return (
    <div
      className="fixed inset-0 bg-black/65 flex items-center justify-center z-50"
      onClick={(e) => e.target === e.currentTarget && !loading && onClose()}
    >
      <div className="bg-[#1a1a1a] border border-[#333] rounded-lg p-6 w-[440px] max-w-[90vw]">
        <h2 className="text-base font-bold text-[#ddd]">
          Add a camera by IP address
        </h2>
        <p className="text-xs text-[#888] mt-1 mb-4 leading-relaxed">
          For cameras that didn't show up automatically. You'll need the
          camera's IP address and the username and password from its app.
        </p>

        {/* IP + Port row */}
        <div className="flex gap-2 mb-3">
          <div className="flex-1">
            <label className="block text-xs font-medium text-[#888] mb-1">
              IP address
            </label>
            <input
              ref={ipRef}
              type="text"
              value={ip}
              onChange={(e) => {
                setIp(e.target.value);
                setError(null);
              }}
              placeholder="10.0.0.42"
              className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500 font-mono"
              onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
            />
          </div>
          <div className="w-24">
            <label className="block text-xs font-medium text-[#888] mb-1">
              Port
            </label>
            <input
              type="text"
              inputMode="numeric"
              value={port}
              onChange={(e) => {
                setPort(e.target.value);
                setError(null);
              }}
              placeholder="554"
              className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500 font-mono"
              onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
            />
          </div>
        </div>

        {/* Username + Password row */}
        <div className="flex gap-2 mb-3">
          <div className="flex-1">
            <label className="block text-xs font-medium text-[#888] mb-1">
              Username
            </label>
            <input
              type="text"
              value={username}
              onChange={(e) => {
                setUsername(e.target.value);
                setError(null);
              }}
              className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500"
            />
          </div>
          <div className="flex-1">
            <label className="block text-xs font-medium text-[#888] mb-1">
              Password
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                setError(null);
              }}
              className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500 font-mono"
              onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
            />
          </div>
        </div>

        {/* Brand hint — optional */}
        <div className="mb-3">
          <label className="block text-xs font-medium text-[#888] mb-1">
            Brand <span className="text-[#555]">(optional)</span>
          </label>
          <select
            value={brand}
            onChange={(e) => setBrand(e.target.value)}
            className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500"
          >
            <option value="">Not sure — we'll figure it out</option>
            {BRANDS_WITH_PATHS.map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
          <p className="text-[11px] text-[#555] mt-1 leading-snug">
            Helps us connect faster. Leave as "Not sure" if you don't know.
          </p>
        </div>

        {/* Advanced toggle — stream path + custom name */}
        <button
          type="button"
          onClick={() => setShowAdvanced(!showAdvanced)}
          className="text-[11px] text-[#666] hover:text-[#ddd] mb-2 transition-colors"
        >
          {showAdvanced ? "− Hide advanced options" : "+ Advanced options"}
        </button>

        {showAdvanced && (
          <div className="border-l-2 border-[#2a2a2a] pl-3 mb-3 space-y-3">
            <div>
              <label className="block text-xs font-medium text-[#888] mb-1">
                Stream path <span className="text-[#555]">(optional)</span>
              </label>
              <input
                type="text"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="/Streaming/Channels/101"
                className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500 font-mono"
              />
              <p className="text-[11px] text-[#555] mt-1 leading-snug">
                Only if you know the exact path. Starts with "/".
              </p>
            </div>

            <div>
              <label className="block text-xs font-medium text-[#888] mb-1">
                Name <span className="text-[#555]">(optional)</span>
              </label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Driveway, Garage, etc."
                className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500"
              />
            </div>
          </div>
        )}

        {error && (
          <div className="mb-3 p-2.5 bg-red-500/10 border border-red-500/30 rounded text-xs text-red-300 leading-relaxed">
            {error}
          </div>
        )}

        <div className="flex gap-2 mt-4">
          <button
            onClick={onClose}
            disabled={loading}
            className="flex-1 px-4 py-2 bg-[#222] border border-[#333] text-[#ddd] text-[13px] font-semibold rounded-md hover:bg-[#2a2a2a] transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="flex-1 px-4 py-2 bg-blue-500 text-white text-[13px] font-semibold rounded-md hover:bg-blue-600 transition-colors disabled:opacity-50 disabled:bg-[#222] disabled:text-[#555]"
          >
            {loading ? "Signing in…" : "Add camera"}
          </button>
        </div>
      </div>
    </div>
  );
}
