# MFi / HomeKit Risk-Surface Brief — SimpleNVR HAP-python bridge

**Not legal advice.** A triage of publicly known facts for counsel, compiled 2026-04-18. Every claim cites a URL; gaps are flagged.

## 1. Apple's stated MFi / HomeKit posture (2024–2026)

- Apple's public **HomeKit Accessory Protocol (HAP) Specification, Non-Commercial Version Release R2** (HAP-R2) was the last freely-distributable spec. It was published via the developer program (under clickwrap, not public CDN) and was last updated July 2019. It is no longer linked from Apple's site. Third-party archives circulate. ([HAP-R2 archived PDF](https://forum.iobroker.net/assets/uploads/files/1634848447889-apple-spezifikation-homekit.pdf), [HomeSpan background](https://github.com/HomeSpan/HomeSpan))
- Apple publishes the [HomeKit Open Source ADK](https://github.com/apple/HomeKitADK) under Apache 2.0, but the README explicitly limits it to **"prototype non-commercial smart home accessories"**; the commercial ADK remains MFi-gated.
- [MFi Program enrollment](https://mfi.apple.com/en/enroll) requires a registered business entity, D-U-N-S number, identity verification, and execution of the MFi NDA. Annual program fee is not the same as the Developer Program's $99; MFi is a separate license with a confidential fee schedule behind the NDA, plus per-product certification costs. ([MFi FAQ](https://mfi.apple.com/en/faqs.html))
- Since January 2025, Apple will **accept CSA Matter certification in lieu of an Apple-specific test** for the "Works with Apple Home" badge — but only for Matter accessories in supported categories (cameras are **not** currently a supported category). ([AppleInsider 2025-01-06](https://appleinsider.com/articles/25/01/06/works-with-apple-home-badge-will-be-easier-for-vendors-to-get), [Apple badge guidelines](https://developer.apple.com/apple-home/works-with-apple-home/))
- **Not publicly answerable from free sources**: exact MFi HomeKit fee, per-unit royalty (if any), and current enrollment lead time. These are NDA'd.

## 2. Precedent — Homebridge / HAP-NodeJS / HAP-python

- [Homebridge](https://github.com/homebridge/homebridge) (Apache 2.0) and [HAP-NodeJS](https://github.com/homebridge/HAP-NodeJS) have shipped publicly since 2015. HAP-NodeJS docs explicitly state it is **not an Apple-certified HAP implementation** and the `This accessory is not certified…` dialog appears to every pairing user. ([HAP-NodeJS issue #338](https://github.com/homebridge/HAP-NodeJS/issues/338))
- [HAP-python (ikalchev/HAP-python)](https://github.com/ikalchev/HAP-python) is Apache 2.0, the library Home Assistant's HomeKit bridge uses, actively maintained through 2026.
- Apple's only known DMCA action in this space is [2014-11-04](https://github.com/github/dmca/blob/master/2014/2014-11-04-Apple.md), targeting "unauthorized copying and distribution of large portions of Apple's HomeKit program specifications" — i.e., leaked NDA specification text, not a clean-room reimplementation. Apple's [2022-01-12 takedown](https://github.com/github/dmca/blob/master/2022/01/2022-01-12-apple.md) was unrelated (internal macOS tools).
- No public DMCA, C&D, or App Store removal targeting Homebridge, HAP-NodeJS, HAP-python, or their forks has been documented in 2015–2026. The "not certified" pairing warning functions as Apple's de facto enforcement channel.

## 3. Precedent — HOOBS (hoobs.com)

- HOOBS is a paid commercial appliance (HOOBS Box ~$169; HOOBS Pro ~$399–$499) bundling a Homebridge-compatible HAP stack. ([HOOBS Pro page](https://hoobs.com/pro/), [iMore review](https://www.imore.com/hoobs-starter-kit-review))
- **Not MFi certified.** HOOBS pairs as an uncertified bridge; users see the same Apple dialog. ([Stacey on IoT review](https://staceyoniot.com/hoobs-review-homekit-homebridge/))
- HOOBS Inc. is still operating as a company in 2026 (website live, copyright 2026), but [Trustpilot reviews](https://www.trustpilot.com/review/hoobs.org) report years-long fulfillment failures. Business health is precarious; legal posture is unchanged.

## 4. Precedent — other paid commercial HAP users

- **Scrypted NVR** ([scrypted.app](https://www.scrypted.app/), [billing](https://billing.scrypted.app/)). Paid subscription ($40/yr first 4 cameras, $10/yr each additional); Desktop App requires a purchased license. Ships HomeKit + HomeKit Secure Video via uncertified HAP. Not MFi certified. Actively sold 2026.
- **Starling Home Hub** ([starlinghome.io](https://www.starlinghome.io/)). Paid hardware (~$89–$99), Nest-to-HomeKit bridge built on the `homebridge-nest` codebase, uncertified, pairs with the non-certified warning. [Discontinued September 2025](https://piunikaweb.com/2025/09/17/starling-home-home-discontinued/) — reason given was US tariffs on small manufacturers, **not** Apple enforcement. Ran as a paid commercial product for ~6 years without action.
- **Home Assistant HomeKit bridge** uses HAP-python in a freely distributed open-source project but ships through [Nabu Casa](https://www.nabucasa.com/)'s paid cloud subscription — another paid commercial context with no public Apple pushback.

## 5. The "commercial shift" risk

- Evidence **for** tolerance extending to paid commercial: HOOBS (6+ yrs, hardware sales), Starling (6 yrs paid hardware), Scrypted (paid subscription + license), Nabu Casa (paid subscription bundling HAP-python). None have been the subject of a publicly-known Apple enforcement action 2020–2026.
- Evidence **against**: Apple has actively DMCA'd NDA'd specification leaks (2014) and continues to quietly issue DMCAs against unrelated private-spec leaks (e.g., the [2025-11-05 takedown](https://github.com/github/dmca/blob/master/2025/11/2025-11-05-apple.md) covering 8,270 repositories). Apple's patent portfolio on HAP is not public. A clean-room reimplementation defense depends on HAP-NodeJS / HAP-python's origin lineage, which has never been tested in court.
- The uncertified-pairing dialog is unambiguously an Apple-engineered gate, which cuts two ways: it is the mechanism by which Apple lets uncertified bridges coexist without enforcement, **and** it is evidence Apple knows about them and has chosen not to escalate.

## 6. Trademark risk (distinct from MFi)

Per [Apple's badge guidelines](https://developer.apple.com/apple-home/works-with-apple-home/) and [general trademark guidelines](https://www.apple.com/legal/intellectual-property/guidelinesfor3rdparties.html):

- **Plausibly red** (affirmatively triggers trademark action): using the "Works with Apple Home" badge artwork; claiming "HomeKit certified"; putting "HomeKit," "Apple Home," or "Siri" in a product name, SKU, or domain; using the Apple logo or house marks.
- **Plausibly yellow** (nominative-fair-use territory, but Apple's written guidelines disallow it for uncertified products): phrasings like "Works with Apple Home," "Supports Apple Home," "HomeKit-compatible." Apple's own wording ties these to *Matter-certified* or *MFi* accessories only.
- **Plausibly green** (descriptive, non-source-identifying, widely used in uncertified-bridge marketing without action): "Pair with Apple Home app," "Stream to the Apple Home app," "View your cameras in the Home app on iPhone and iPad." Referential use naming the app in running body copy, with no badge and no certification claim, is the standard Homebridge/HOOBS/Scrypted pattern.

## 7. Triage summary for counsel

- **Plausibly green.** Shipping an MIT/Apache-licensed HAP-python-based bridge inside a paid SimpleNVR product, pairing with the uncertified-accessory warning intact, using only descriptive "pair in the Home app" language with no Apple badge artwork. Six-plus years of HOOBS/Starling/Scrypted/Nabu Casa precedent, and HAP-python itself is in a project (Home Assistant) with enterprise commercial distribution.
- **Plausibly yellow.** Marketing that uses "Works with Apple Home," "HomeKit-compatible," or "Apple Home" in product name, SKU, or prominent hero copy. Bundling Apple-NDA'd spec text (e.g., HAP-R2 PDF) into the repo or installer. Shipping the HAP code itself as a public SimpleNVR-branded npm/PyPI package with SimpleNVR-first framing rather than as a dependency on HAP-python. Using HAP over any transport beyond what the spec contemplates (e.g., cloud-relayed accessory).
- **Plausibly red.** Using "Works with Apple Home" badge artwork without the Matter/MFi certification that Apple's badge license requires. Putting "HomeKit" in SimpleNVR's product name or trademarked logo. Distributing Apple's MFi ADK source, leaked specifications, or any NDA'd Apple material. Representing to users that SimpleNVR is "Apple-certified" when it is not.

**Open questions for counsel.** (a) Does Apple's acceptance of the uncertified-pairing path constitute an implied license or merely forbearance? (b) Are any Apple HomeKit-related patents likely readable on a HAP-python bridge, and what's the clean-room provenance of HAP-python/HAP-NodeJS? (c) Does paid-subscription monetization of the bridge (vs. one-time license) change the analysis under Apple's developer agreements Ben has signed for the App Store / Developer Program?
