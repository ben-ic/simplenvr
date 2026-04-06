import { useEffect, useRef, useState } from "react";
import { submitAuth } from "../api/client";
import { getAuthHint } from "../authHints";
import type { Camera } from "../types";

export function AuthModal({
  camera,
  onClose,
}: {
  camera: Camera;
  onClose: () => void;
}) {
  const hint = getAuthHint(camera.manufacturer);
  const [username, setUsername] = useState(camera.username || "admin");
  const [password, setPassword] = useState("");
  const [applyAll, setApplyAll] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const passwordRef = useRef<HTMLInputElement>(null);

  // Autofocus password and handle ESC
  useEffect(() => {
    passwordRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const displayName =
    camera.name ||
    [camera.manufacturer, camera.model].filter(Boolean).join(" ") ||
    "Unknown Camera";

  const handleSubmit = async () => {
    if (!password) return;
    setLoading(true);
    setError("");
    try {
      const result = await submitAuth(camera.id, username, password, applyAll);
      if (result.status === "needs_auth") {
        setError("Invalid credentials — double-check the password in the camera's app");
        setLoading(false);
      } else {
        onClose();
      }
    } catch {
      setError("Connection failed");
      setLoading(false);
    }
  };

  return (
    <div
      className="fixed inset-0 bg-black/65 flex items-center justify-center z-50"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="bg-[#1a1a1a] border border-[#333] rounded-lg p-6 w-[420px] max-w-[90vw]">
        <h2 className="text-base font-bold text-[#ddd]">
          Connect to {camera.manufacturer || "Camera"}
        </h2>
        <p className="text-xs text-[#888] mb-4">
          {displayName} &middot; {camera.ip}
        </p>

        {/* Manufacturer-specific instructions */}
        <div className="mb-4 p-3 bg-blue-500/10 border border-blue-500/20 rounded text-xs text-blue-200/90 leading-relaxed">
          {hint.passwordHint}
        </div>

        <div className="mb-3.5">
          <label className="block text-xs font-medium text-[#888] mb-1">
            Username
          </label>
          <input
            type="text"
            value={username}
            onChange={(e) => {
              setUsername(e.target.value);
              setError("");
            }}
            className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500"
          />
          <p className="text-[11px] text-[#555] mt-1">{hint.username}</p>
        </div>

        <div className="mb-3.5">
          <label className="block text-xs font-medium text-[#888] mb-1">
            Password
          </label>
          <input
            ref={passwordRef}
            type="password"
            value={password}
            onChange={(e) => {
              setPassword(e.target.value);
              setError("");
            }}
            placeholder="Camera password"
            className="w-full px-2.5 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500 placeholder:text-[#555]"
            onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
          />
        </div>

        {camera.manufacturer && (
          <label className="flex items-center gap-1.5 text-xs text-[#888] mt-4 mb-4 cursor-pointer">
            <input
              type="checkbox"
              checked={applyAll}
              onChange={(e) => setApplyAll(e.target.checked)}
              className="accent-blue-500"
            />
            Apply to all {camera.manufacturer} cameras
          </label>
        )}

        {error && (
          <p className="text-xs text-red-400 mb-3 leading-relaxed">{error}</p>
        )}

        <div className="flex gap-2">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 bg-[#222] border border-[#333] text-[#ddd] text-[13px] font-semibold rounded-md hover:bg-[#2a2a2a] transition-colors"
          >
            Skip
          </button>
          <button
            onClick={handleSubmit}
            disabled={loading || !password}
            className="flex-1 px-4 py-2 bg-blue-500 text-white text-[13px] font-semibold rounded-md hover:bg-blue-600 transition-colors disabled:opacity-50 disabled:bg-[#222] disabled:text-[#555]"
          >
            {loading ? "Connecting..." : "Connect"}
          </button>
        </div>
      </div>
    </div>
  );
}
