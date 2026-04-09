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
  const [showPassword, setShowPassword] = useState(false);
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
        setError(
          "That password didn't work — check the camera's app and try again.",
        );
        setLoading(false);
      } else {
        onClose();
      }
    } catch {
      setError(
        "Couldn't reach the camera. Check that it's powered on and on the same network.",
      );
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
          Sign in to {camera.manufacturer || "Camera"}
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
          <div className="relative">
            <input
              ref={passwordRef}
              type={showPassword ? "text" : "password"}
              value={password}
              onChange={(e) => {
                setPassword(e.target.value);
                setError("");
              }}
              placeholder="Camera password"
              className="w-full pl-2.5 pr-9 py-[7px] bg-[#111] border border-[#333] rounded text-sm text-[#ddd] outline-none focus:border-blue-500 placeholder:text-[#555] font-mono"
              onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
            />
            <button
              type="button"
              onClick={() => setShowPassword((v) => !v)}
              className="absolute right-1 top-1/2 -translate-y-1/2 p-1.5 text-[#666] hover:text-[#ddd] transition-colors"
              tabIndex={-1}
              aria-label={showPassword ? "Hide password" : "Show password"}
            >
              {showPassword ? (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                  <line x1="1" y1="1" x2="23" y2="23" />
                </svg>
              ) : (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                  <circle cx="12" cy="12" r="3" />
                </svg>
              )}
            </button>
          </div>
        </div>

        {camera.manufacturer && (
          <label className="flex items-center gap-1.5 text-xs text-[#888] mt-4 mb-4 cursor-pointer">
            <input
              type="checkbox"
              checked={applyAll}
              onChange={(e) => setApplyAll(e.target.checked)}
              className="accent-blue-500"
            />
            Use this login for all my {camera.manufacturer} cameras
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
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={loading || !password}
            className="flex-1 px-4 py-2 bg-blue-500 text-white text-[13px] font-semibold rounded-md hover:bg-blue-600 transition-colors disabled:opacity-50 disabled:bg-[#222] disabled:text-[#555]"
          >
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </div>
      </div>
    </div>
  );
}
