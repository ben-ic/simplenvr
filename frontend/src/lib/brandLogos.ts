/**
 * Brand logo URL lookup — shared between onboarding and discovery UI.
 *
 * Reads manifest.json produced by scripts/fetch_brand_logos.py and
 * resolves each entry to a Vite-bundled asset URL via glob import.
 * The manifest key is the brand's display name (as it appears in
 * backend/discovery/fingerprints.py), so callers can pass whatever
 * manufacturer string the backend returned and get back a URL or
 * null without any name-mangling.
 *
 * Logos are only rendered in identification contexts (onboarding
 * tiles, discovered-camera cards). Never in SimpleNVR marketing or
 * anywhere that could imply endorsement. See docs/trademarks.md for
 * the full legal rationale.
 */

import logoManifest from "../assets/brand-logos/manifest.json";

// Vite glob import: pulls every SVG in the brand-logos folder as a URL
// the bundler will serve. The keys are relative paths like
// "../assets/brand-logos/reolink.svg" and the values are resolved URLs.
const logoModules = import.meta.glob<string>("../assets/brand-logos/*.svg", {
  eager: true,
  import: "default",
  query: "?url",
});

const logoUrlByFilename: Record<string, string> = Object.fromEntries(
  Object.entries(logoModules).map(([path, url]) => [
    path.split("/").pop() || "",
    url as unknown as string,
  ])
);

/**
 * Look up a bundled logo URL for a brand display name.
 *
 * Returns null if no logo is bundled for that brand. Callers should
 * fall back to a text-only tile (first-letter avatar or similar).
 *
 * Name matching is exact against the manifest keys, which in turn
 * match the `brand` field in backend/discovery/fingerprints.py. A
 * backend-returned manufacturer like "TP-Link Tapo" will match the
 * manifest entry "TP-Link Tapo"; "Tapo" alone won't match unless we
 * add an alias.
 */
export function getBrandLogoUrl(brandName: string | null | undefined): string | null {
  if (!brandName) return null;
  const filename = (logoManifest as Record<string, string>)[brandName];
  if (!filename) return null;
  return logoUrlByFilename[filename] || null;
}
