# SimpleNVR

A desktop NVR (Network Video Recorder) for IP cameras. You install it, it finds your cameras, and it records. That's the pitch.

---

## Who this is for

Not security integrators. Not IT professionals. Not datacenter operators.

SimpleNVR is for:

- **Home owners** with a handful of cameras around the house
- **Small business owners** with 8 to 32 cameras — the shop, the office, the warehouse, the farm, the dental practice
- **Rental property owners** keeping an eye on properties from somewhere else

The specific user we build for is a homeowner who bought four Reolink cameras from Costco to keep an eye on the yard. A dentist with six cameras across the waiting room and the parking lot. A farm supply store with cameras on the register, the back lot, and the loading dock.

These users have one thing in common: **they do not want to think about the technology.** They want something that works out of the box, stays out of the way, and doesn't demand IT knowledge to set up or keep running.

When a design decision comes up, we imagine that person standing at their laptop trying to do the thing. If they would get stuck, be scared, or give up — the design is wrong, no matter how clean the implementation is.

---

## What we promise

1. **It finds your cameras.** Plug them into the network. SimpleNVR discovers them automatically — you don't type IP addresses.
2. **It records everything.** Smart storage math means you set a budget (e.g., "use 500 GB") and we manage retention via a rolling buffer.
3. **It runs on your machine.** No cloud. No remote service. Video never leaves your network unless you explicitly share it.
4. **It doesn't phone home.** No telemetry, no analytics, no account required.
5. **It handles crashes.** If something goes wrong, it recovers on its own. The user should never need to know what "sidecar" or "ffmpeg" mean.
6. **It scales to 32 cameras.** Beyond that, we're not the right tool and we'll say so.

---

## How we build it

Every feature passes this test: **"Can a non-technical user do this?"** If the answer is no, we don't ship it. If the answer requires a word a non-technical user doesn't know, we change the word.

Concrete rules that fall out of this test:

### Zero configuration

First launch does the whole setup: discovers cameras on the LAN, identifies them by brand and model where possible, registers them with the recording pipeline, and starts capturing. The only thing the user has to provide is the credentials for their cameras — and we tell them the common defaults to try first.

There are no settings for "recording bitrate," "keyframe interval," "RTSP transport," or "buffer size." Users get no knobs. If a user wants to tune something, they're not our user.

*(Exception: developers get every environment variable they want. Developer escape hatches are fine as long as they're never surfaced in the UI.)*

### Ask the user, but trust the network more

When we ask the user something during onboarding (e.g., "what brand of cameras do you have?"), their answer is a **hint**, never a filter. We use it to rank matches higher when the network signals are ambiguous, but we never suppress a clear auto-detected identification just because the user didn't declare that brand.

People forget what they own. People inherit cameras from previous tenants. People buy new cameras and don't revisit onboarding. If SimpleNVR detects an Annke camera on a network where the user said "I only have Reolinks," the right answer is:

> **Annke C500** — *found on your network (you didn't mark Annke when you set up)*

That tiny honesty turns "huh?" into "oh right, I forgot about that one." The user learns something. The app feels smart, not contradictory.

The rule: **declaration boosts, network detects, honest UI reconciles.**

### Tell the user what's happening

Never show a blank screen. Never show a spinner without text. Never show an error code without an explanation.

- "Connecting to your camera..." is infinitely better than a spinning wheel
- "This only happens once on first connect" is infinitely better than silent waiting
- "Can't reach your Reolink — is it powered on?" is infinitely better than "Error 500"

If the user is going to wait, tell them why and roughly how long. If the user is going to see something unexpected, explain it before they see it.

### Fail loudly, fail helpfully

When something genuinely breaks, say so in plain language, and say what the user can do about it. An error message is a UX surface, not a debugging log. The debugging log belongs in a file the user never has to open.

Every error message answers two questions:
1. *What went wrong?* (in the user's words, not ours)
2. *What should I do now?* (specific and actionable)

### Recover silently

If something breaks and recovers on its own, the user should never find out. Automatic ffmpeg restart, camera reconnection after a network blip, segment file cleanup — all invisible. Crash dialogs are reserved for things the user has to act on.

The user's mental model is *"my NVR just works."* Our job is to protect that model from every glitch that doesn't actually require user intervention.

### First principles, from the user's perspective

Every design discussion starts with *"what does the user see, feel, and need to do next?"* — never with *"what's the cleanest implementation?"*

If we catch ourselves proposing an architecture before we've described the user's experience, we stop and start over.

---

## Battery-powered motion-wake cameras

Some wireless cameras are battery-powered and sleep between motion events to save power. When they're asleep, they're not on the network — there's nothing for SimpleNVR to connect to. Whether this is a real limitation depends on whether the cameras have a hub:

**With a hub** (Eufy HomeBase, Reolink Home Hub, Arlo SmartHub): the hub stays connected to the cameras 24/7, buffers motion clips, and re-broadcasts them on the LAN. SimpleNVR connects to the hub's RTSP output, not to the individual cameras. This works well today for Eufy HomeBase (firmware 1.0.7.4+) and Reolink hubs. The UI should show these cameras in an "asleep — will wake on motion" state between wake events, not as "offline."

**Without a hub** (direct-to-cloud battery cameras): see "What we don't do" below — these are cloud-only and therefore out of scope.

## What we don't do

Just as important as what we promise is what we explicitly refuse to build.

- **We don't do cloud.** Your video is yours. Any feature that requires us to host anything is off the roadmap.
- **We don't support cloud-only cameras.** Cameras from Ring, Blink, Google Nest, Amazon, stock Wyze, TP-Link Kasa, Arlo (without a local hub), and Xiaomi stream exclusively through the manufacturer's cloud service and don't expose a local video feed. SimpleNVR records what's on your network — if a camera's video never touches your network, we can't record it. These brands are not listed in the onboarding flow, not identified by the fingerprint pipeline, and not supported by any future roadmap item. Users who own these brands should keep using the manufacturer's own app. This is a deliberate scope decision, not a limitation we intend to fix.
- **We don't do subscriptions.** One-time purchase, zero recurring fees. *(Long-term, the business model could become a subscription or stay a one-time charge — that decision is open. Whichever it becomes, the core NVR will never require a recurring fee to keep working.)*
- **We don't do 100+ cameras.** That's a datacenter problem with different trade-offs (batch hardware decode, clustered storage, SSO). Not our fight.
- **We don't do AI bells and whistles.** Basic motion detection yes. Face recognition, license-plate reading, person tracking, behavior analysis — not the product. Users who need those have different priorities and bigger budgets.
- **We don't compete on feature count.** We compete on *does it work without thinking*. Every feature we add has to clear the "does a non-technical user need this?" bar.
- **We don't do custom scripts, rules engines, or automation DSLs.** If a user needs to write logic to configure their NVR, they're not our user.
- **We don't do enterprise IT integrations.** Active Directory, LDAP, SSO, SCIM, role-based access control — all scope-creep traps that pull us toward an audience we don't serve.

---

## What "commercial" means here

SimpleNVR is built to be a commercial product. That shapes real decisions:

- **License-clean dependencies.** Every third-party component must be MIT, Apache, BSD, or LGPL via subprocess. No GPL, no AGPL. No "non-commercial use" licenses. Commercial redistribution has to be legally straightforward.
- **No references to competitors.** The repo, the code, the commits, and the UI never mention other NVR products by name. We're building our own thing, not defining ourselves against someone else's.
- **Code signing and notarization are first-class concerns.** An unsigned binary is a non-starter for the target audience — the OS will scare them away before they can click "open anyway."
- **Crash-proof lifecycle.** The app must survive any form of death without leaving orphan processes or corrupted recordings. This is a commercial-grade requirement, not a nice-to-have.

---

## How to read this document

When there's a disagreement about what to build or how to build it, this document wins. If a proposal conflicts with anything here, the proposal changes — not the document.

When this document needs to change, the change is deliberate. Adding a "We don't do X" line is easy. Removing one is a real decision.
