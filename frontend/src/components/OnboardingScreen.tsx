import { useState } from "react";
import { apiFetch } from "../lib/backend";
import { getBrandLogoUrl } from "../lib/brandLogos";

/**
 * First-launch brand selection screen.
 *
 * The user sees a grid of camera brands and checks any they own. The
 * selection is a HINT to the backend fingerprint identifier — it boosts
 * confidence when a network scan matches a declared brand, but never
 * suppresses detection of a brand the user didn't declare (users forget
 * what they own, inherit cameras, or add new brands without revisiting
 * this screen; see docs/product.md "Ask the user, but trust the network
 * more").
 *
 * The user can skip this screen entirely. Skipping is a first-class
 * option, not a hidden "Continue without selecting" link. Users who
 * don't know what they have should feel respected, not pressured.
 *
 * Brands whose cameras stream exclusively through the manufacturer's
 * cloud (Ring, Google Nest, stock Wyze, TP-Link Kasa, Arlo without a
 * hub, Xiaomi) are intentionally NOT in the list below. SimpleNVR
 * cannot record from those cameras, and listing them would either
 * mislead the user (implying support) or confuse them (greyed-out
 * tiles invite "is this broken?" questions). The honest move is to
 * not mention them here. Users who own those brands will figure out
 * quickly that SimpleNVR isn't the right tool, which is the correct
 * outcome for them. See docs/product.md "What we honestly can't do."
 */

// Brand metadata. This is the MVP hard-coded list; once the fingerprint
// database (backend/discovery/fingerprints.py) is populated, we'll fetch
// this list from a backend endpoint so brand additions don't require
// a frontend rebuild.
interface BrandOption {
  id: string;       // stable identifier used by the backend
  name: string;     // display name
  tier: 1 | 2 | 3;  // 1 = consumer, 2 = prosumer/SMB, 3 = niche
  note?: string;    // optional setup hint shown as tooltip
}

const BRANDS: BrandOption[] = [
  // Tier 1 — home consumer (locally-recordable only)
  { id: "Reolink", name: "Reolink", tier: 1 },
  { id: "Eufy", name: "Eufy", tier: 1, note: "Requires Eufy HomeBase hub with RTSP enabled per-camera in the Eufy app." },
  { id: "Tapo", name: "TP-Link Tapo", tier: 1 },
  { id: "UniFi", name: "Ubiquiti UniFi Protect", tier: 1 },
  { id: "Amcrest", name: "Amcrest", tier: 1 },

  // Tier 2 — prosumer / SMB
  { id: "Hikvision", name: "Hikvision", tier: 2 },
  { id: "Dahua", name: "Dahua", tier: 2 },
  { id: "Axis", name: "Axis Communications", tier: 2 },
  { id: "Uniview", name: "Uniview (UNV)", tier: 2 },
  { id: "Lorex", name: "Lorex", tier: 2 },
  { id: "Swann", name: "Swann", tier: 2 },
  { id: "Foscam", name: "Foscam", tier: 2 },
  { id: "Annke", name: "Annke", tier: 2 },

  // Tier 3 — regional / niche
  { id: "Bosch", name: "Bosch Security", tier: 3 },
  { id: "Hanwha", name: "Hanwha Wisenet", tier: 3 },
  { id: "Pelco", name: "Pelco", tier: 3 },
  { id: "Avigilon", name: "Avigilon", tier: 3 },
  { id: "Vivotek", name: "Vivotek", tier: 3 },
  { id: "Mobotix", name: "Mobotix", tier: 3 },
  { id: "i-PRO", name: "i-PRO (Panasonic)", tier: 3 },
  { id: "GeoVision", name: "GeoVision", tier: 3 },
  { id: "Ezviz", name: "Ezviz", tier: 3, note: "Ezviz cameras typically use their device verification code (on the label) as the RTSP password." },
];

export function OnboardingScreen({
  onContinue,
}: {
  onContinue: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showOtherBrand, setShowOtherBrand] = useState(false);
  const [otherBrandText, setOtherBrandText] = useState("");

  const toggle = (brandId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(brandId)) next.delete(brandId);
      else next.add(brandId);
      return next;
    });
  };

  const save = async (declaredBrands: string[]) => {
    setSaving(true);
    setError(null);
    try {
      // We need the full Settings object to POST, not just our field,
      // because the backend endpoint replaces everything. Fetch current
      // first, merge our changes, then PUT.
      const currentResp = await apiFetch("/api/settings");
      if (!currentResp.ok) throw new Error("Failed to load current settings");
      const current = await currentResp.json();
      const updated = {
        ...current,
        onboarding_completed: true,
        declared_brands: declaredBrands,
      };
      const resp = await apiFetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updated),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`Save failed: ${text || resp.statusText}`);
      }
      onContinue();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
    }
  };

  const handleContinue = () => save([...selected]);
  const handleSkip = () => save([]);

  const tier1 = BRANDS.filter((b) => b.tier === 1);
  const tier2 = BRANDS.filter((b) => b.tier === 2);
  const tier3 = BRANDS.filter((b) => b.tier === 3);

  return (
    <div className="flex-1 flex flex-col bg-[#0a0a0a] text-[#ddd] overflow-auto">
      {/* Header */}
      <div className="max-w-3xl mx-auto w-full px-6 pt-12 pb-6">
        <div className="text-xs font-semibold uppercase tracking-widest text-[#666] mb-2">
          Welcome to SimpleNVR
        </div>
        <h1 className="text-2xl font-semibold text-white mb-3">
          What brand(s) of cameras do you have?
        </h1>
        <p className="text-sm text-[#888] leading-relaxed max-w-xl">
          Don't worry if you're not sure — we'll do our best to identify your
          cameras automatically when we scan your network. Telling us the
          brand just helps us set them up faster and show you the right
          setup hints.
        </p>
      </div>

      {/* Brand grid */}
      <div className="max-w-3xl mx-auto w-full px-6 pb-8 flex-1">
        <BrandGroup
          title="Common home cameras"
          brands={tier1}
          selected={selected}
          onToggle={toggle}
        />
        <BrandGroup
          title="Business & prosumer"
          brands={tier2}
          selected={selected}
          onToggle={toggle}
        />
        <BrandGroup
          title="Other"
          brands={tier3}
          selected={selected}
          onToggle={toggle}
        />

        {/* "I don't see my brand" link */}
        <button
          onClick={() => setShowOtherBrand(true)}
          className="mt-2 text-xs text-[#888] hover:text-[#ddd] transition-colors"
        >
          I don't see my brand →
        </button>

        {showOtherBrand && (
          <div className="mt-4 p-4 bg-[#141414] border border-[#333] rounded">
            <div className="text-sm text-[#ddd] mb-2">
              What brand are your cameras?
            </div>
            <p className="text-xs text-[#888] mb-3">
              We'll still try to identify them automatically. Typing the
              brand here just lets us know to add better support for it.
            </p>
            <input
              type="text"
              value={otherBrandText}
              onChange={(e) => setOtherBrandText(e.target.value)}
              placeholder="Brand name"
              className="w-full bg-[#0a0a0a] border border-[#333] text-[#ddd] rounded px-3 py-2 text-sm focus:border-[#555] focus:outline-none"
            />
            <div className="mt-3 flex gap-2">
              <button
                onClick={() => {
                  const trimmed = otherBrandText.trim();
                  if (trimmed) {
                    setSelected((prev) => new Set([...prev, trimmed]));
                  }
                  setOtherBrandText("");
                  setShowOtherBrand(false);
                }}
                className="px-3 py-1.5 bg-[#222] border border-[#333] rounded text-xs font-semibold text-[#ddd] hover:bg-[#2a2a2a]"
              >
                Add
              </button>
              <button
                onClick={() => {
                  setOtherBrandText("");
                  setShowOtherBrand(false);
                }}
                className="px-3 py-1.5 text-xs text-[#888] hover:text-[#ddd]"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Footer with skip + continue */}
      <div className="border-t border-[#333] bg-[#141414] sticky bottom-0">
        <div className="max-w-3xl mx-auto w-full px-6 py-4 flex items-center justify-between gap-4">
          <div className="text-xs text-[#666]">
            {selected.size === 0
              ? "Nothing selected — that's fine, we'll auto-detect"
              : `${selected.size} brand${selected.size === 1 ? "" : "s"} selected`}
          </div>
          <div className="flex items-center gap-3">
            {error && (
              <div className="text-xs text-red-500 max-w-xs truncate" title={error}>
                {error}
              </div>
            )}
            <button
              onClick={handleSkip}
              disabled={saving}
              className="px-4 py-2 text-xs font-medium text-[#888] hover:text-[#ddd] transition-colors disabled:opacity-50"
            >
              Skip / I don't know
            </button>
            <button
              onClick={handleContinue}
              disabled={saving}
              className="px-5 py-2 bg-white text-black text-xs font-semibold rounded hover:bg-[#e5e5e5] transition-colors disabled:opacity-50"
            >
              {saving ? "Saving…" : "Continue →"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function BrandGroup({
  title,
  brands,
  selected,
  onToggle,
}: {
  title: string;
  brands: BrandOption[];
  selected: Set<string>;
  onToggle: (brandId: string) => void;
}) {
  if (brands.length === 0) return null;
  return (
    <div className="mb-6">
      <div className="text-[11px] font-semibold uppercase tracking-wider text-[#555] mb-2">
        {title}
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
        {brands.map((brand) => (
          <BrandTile
            key={brand.id}
            brand={brand}
            selected={selected.has(brand.id)}
            onClick={() => onToggle(brand.id)}
          />
        ))}
      </div>
    </div>
  );
}

function BrandTile({
  brand,
  selected,
  onClick,
}: {
  brand: BrandOption;
  selected: boolean;
  onClick: () => void;
}) {
  const logoUrl = getBrandLogoUrl(brand.name);
  return (
    <button
      onClick={onClick}
      title={brand.note || undefined}
      className={`group relative text-left px-3 py-3 border rounded transition-all ${
        selected
          ? "bg-[#1a1a1a] border-white text-white"
          : "bg-[#141414] border-[#333] text-[#ddd] hover:border-[#555] hover:bg-[#1a1a1a]"
      }`}
    >
      <div className="flex items-center gap-2.5">
        {/* Logo column: fixed width so text-only and logo tiles line up
            visually in the grid. Logos are rendered as <img> rather than
            inlined <svg> because they come from a bundler glob import. */}
        <div className="w-7 h-7 shrink-0 flex items-center justify-center">
          {logoUrl ? (
            <img
              src={logoUrl}
              alt=""
              className="max-w-full max-h-full object-contain opacity-90 group-hover:opacity-100"
              /* Logos use nominative fair use (see docs/trademarks.md).
                 Rendered at small size as identification hints only. */
            />
          ) : (
            <div className="w-full h-full rounded border border-[#444] text-[10px] font-semibold text-[#888] flex items-center justify-center">
              {brand.name.charAt(0)}
            </div>
          )}
        </div>
        <span className="text-sm font-medium truncate flex-1">{brand.name}</span>
        {selected && (
          <svg
            className="w-3.5 h-3.5 text-white shrink-0"
            fill="none"
            stroke="currentColor"
            strokeWidth={3}
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
          </svg>
        )}
      </div>
    </button>
  );
}
