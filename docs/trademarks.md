# Trademarks and third-party identification

SimpleNVR identifies IP cameras on a local network by showing the user the brand name and, where possible, the brand's logo. The brand names and logos referenced in this application are trademarks of their respective owners. This document explains how we use them and why that use is lawful.

## The specific use case

During first-launch setup, SimpleNVR shows the user a grid of camera brand tiles so they can tell us which brands of camera they own. Later, after discovering devices on the LAN, SimpleNVR shows a card for each detected camera with the brand name and logo so the user can visually match the card to the physical camera on their wall. Nothing else in SimpleNVR uses any third-party brand marks — not the product icon, not the installer, not the marketing, not documentation headers.

## Why this is lawful

This use falls under the doctrine of **nominative fair use**, a well-established trademark defense under both US federal trademark law and analogous doctrines in the EU, UK, Canada, and Australia. In the United States, the doctrine was articulated in [*New Kids on the Block v. News America Publishing* (9th Cir. 1992), 971 F.2d 302](https://caselaw.findlaw.com/court/us-9th-circuit/1363316.html). It has three requirements, all of which SimpleNVR meets:

1. **The product or service cannot be readily identified without using the trademark.** We literally cannot tell a user "you have a Reolink camera on your network" without saying the word "Reolink." We cannot help a user visually recognize which physical camera corresponds to a network card without showing the logo they saw on the box.

2. **Only so much of the mark is used as is reasonably necessary to identify the product or service.** We display each logo once, at small size, on a tile that identifies the brand. We do not use the logos in any promotional context, in the SimpleNVR app icon, or in marketing. We use the same simple rendering for every brand so that no single brand is visually elevated over the others.

3. **The use does nothing that would suggest sponsorship or endorsement by the trademark holder.** SimpleNVR does not claim to be affiliated with, endorsed by, or connected to any of the brands whose cameras it identifies. The UI explicitly presents these brands as distinct third parties whose products SimpleNVR can recognize on the user's network.

This is the same doctrine that permits iFixit to show Apple logos on repair guides, IT asset management tools to show vendor logos in inventory dashboards, home-automation platforms to show device logos on integration cards, and price-comparison sites to show product logos next to listings. Nominative fair use is foundational to any software that interacts with a heterogeneous ecosystem of third-party devices or services.

## Copyright considerations

Brand logos are, separately from trademark law, sometimes protected by copyright. The logos SimpleNVR uses are sourced from [Wikimedia Commons](https://commons.wikimedia.org/). Files on Wikimedia Commons fall into one of three categories:

- **Below the threshold of originality**: a logo that consists only of text, simple shapes, or simple geometry does not qualify for copyright protection under the US "threshold of originality" doctrine. Most brand logos in this project fall into this category.
- **Released under a free license**: some logos are explicitly licensed by their owners for unrestricted use (Creative Commons, public domain dedication, etc.).
- **Permitted under trademark-only status**: Wikimedia Commons will host logos that are trademarks but not copyrighted works, explicitly for identification purposes.

In all three cases, the use is lawful. SimpleNVR downloads each logo from Wikimedia Commons at build time into `frontend/src/assets/brand-logos/` and bundles the resulting SVG files into the app.

## What we don't do

- We do not use any brand's logo in the SimpleNVR app icon
- We do not use any brand's logo in SimpleNVR marketing materials, website, or advertising
- We do not claim affiliation with, endorsement by, or certification from any brand
- We do not use brand logos in contexts where they could cause consumer confusion about the origin of SimpleNVR
- We do not modify the logos (beyond standard resizing for display)
- We do not use brand logos to imply compatibility with SimpleNVR beyond the literal fact that SimpleNVR can detect the cameras' presence on the network

## Trademark notices

All brand names, logos, and trademarks are the property of their respective owners. Their appearance in SimpleNVR does not imply endorsement of SimpleNVR by the trademark holders, nor does it imply any affiliation between SimpleNVR and those entities.

A full list of the brands SimpleNVR identifies, along with the Wikimedia Commons source URLs for each logo, can be found in `backend/discovery/fingerprints.py`.

## Questions or concerns

If you are a brand owner and you would like SimpleNVR to stop using your logo, or to change how it is used, please open an issue on the SimpleNVR GitHub repository. We will respond promptly and respectfully — our goal is to help users find and use your products, not to create friction with you.
