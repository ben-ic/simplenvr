# MAC-fallback IP reconciliation (handle DHCP lease changes)

## Goal
When a known camera's DHCP lease rolls over to a new IP, recognize
it as the same physical device by MAC address, update the stored IP,
re-register the go2rtc source, and bounce the recorder — without
forcing the user into the onboarding flow as if it were a new device,
and without leaving the old IP record stuck offline forever.

## Current behavior (the bug)

`backend/discovery/scanner.py` is IP-keyed end-to-end:

- `_known_cameras: dict[ip, Camera]` — in-memory cache by IP.
- `db.get_camera_by_ip(ip)` — only lookup helper used during scan.
- No `update_camera_ip` or MAC-keyed lookup exists in `backend/db.py`
  (only `update_camera_auth` and `update_camera_name`).

So when a camera moves from `10.0.0.63` to `10.0.0.181` overnight:

1. Scanner sees `10.0.0.181`, doesn't match `_known_cameras`, doesn't
   match `db.get_camera_by_ip(10.0.0.181)` either.
2. It enters the new-camera identification flow. Either it onboards
   as a brand-new device (user sees a duplicate in their list) or
   the auth probe fails and it stays unknown.
3. The original `10.0.0.63` row is still in the DB; its recorder
   keeps trying to connect there; the watchdog flips it permanently
   `offline` because the IP is dead from the camera's perspective.

The MAC address IS already populated at first discovery
(`scanner.py:104-109` reads ARP and writes `mac_address=mac` into the
`IdentifySignals`), it's just never re-consulted.

DHCP reservations on the router are the user-side workaround the
NVR industry recommends (Blue Iris docs lead with it), but
**that violates SimpleNVR's zero-config principle** — the average
target user (mama-and-papa shop) doesn't have credentials to their
own router, let alone a mental model for static leases. We have to
solve this on our side.

## Behavior after fix

When a previously-unknown IP appears with a MAC that matches a
camera already in the DB:

1. **Don't onboard as new.** Treat as the same device that moved.
2. **Update the camera row's IP atomically** — `db.update_camera_ip`
   takes camera_id + new_ip and updates `cameras.ip`,
   `cameras.authed_uri` (re-derived), `cameras.substream_uri`
   (re-derived), and clears `fallback_reason` if it was set to
   "camera unreachable" or similar IP-related reasons.
3. **Re-register the go2rtc source** with the new upstream URL
   (`go2rtc_client.set_camera` already exists for this; it overwrites
   in place, so the loopback URL `<uuid>` stays the same and
   downstream consumers don't need to know).
4. **Bounce the recorder** so its ffmpeg picks up the new go2rtc
   loopback connection (the loopback URL is unchanged but go2rtc's
   upstream just changed; safest to restart so any in-flight session
   tears down cleanly).
5. **Drop the old in-memory cache entry** (`_known_cameras` is
   IP-keyed, so the old key needs explicit deletion alongside the
   new key insertion).
6. **Emit `camera_updated` on the event bus** so the frontend
   refreshes its camera list and the UI shows the new IP in Camera
   Setup.

## Detection mechanism — the key correctness question

When the scanner sees a new IP, look up its MAC in the current ARP
table (`get_arp_table()` already cached). If that MAC matches an
existing camera in the DB whose IP is currently considered offline
or unreachable, treat it as a move.

**False-positive risk:** the MAC has to be from a known *camera*
brand, not just any device. Two safeguards:

- **MAC must be present in ARP.** A new IP without an ARP entry
  (firewalled, asleep, or just not pinged yet) doesn't trigger the
  reconciliation — it goes through normal new-camera flow.
- **Match only against cameras whose recorder is currently failing
  or whose health is `offline`/`record_failing`.** A healthy camera
  still answering at its old IP shouldn't be reassigned just because
  a different device coincidentally shares its MAC (extremely rare
  but conceivable with cloned hardware or VM bridging).

**ARP cache freshness:** `get_arp_table()` is already used in the
scanner, and `invalidate_arp_cache()` exists. The reconciliation
logic should call `invalidate_arp_cache()` once at the start of each
scan loop iteration where it's looking for moved devices, so we
don't act on a 5-minute-stale ARP entry.

## Atomicity / race conditions

The window between "IP change detected" and "recorder bounced" is
where things get messy. Specifically:

- The watchdog might be mid-restart of the offline recorder when the
  scanner triggers a config-update bounce. Without coordination
  these collide and we end up with two ffmpeg generations.
- The frontend might be holding a `<rtsp-tile>` pointed at the
  loopback URL at the moment go2rtc rebinds upstream. The loopback
  URL doesn't change (it's keyed by camera UUID), but the tile may
  see a brief stream interruption.

The recorder's existing `apply_settings_change` / `camera_updated`
event paths already handle in-flight bounces correctly (per the
"Settings propagation invariant" load-bearing notes in
`docs/architecture.md`); the IP-change path should reuse that
plumbing rather than invent a new one.

## Schema

No new column needed. `cameras.mac_address` already exists. We just
need a new query function and a new mutation:

```python
async def get_camera_by_mac(conn, mac: str) -> Camera | None: ...
async def update_camera_ip(
    conn, *, camera_id: str, new_ip: str
) -> bool: ...
```

`update_camera_ip` updates `ip`, re-derives and updates `authed_uri`
and `substream_uri`, and returns True if a row was actually changed.

## UX touch — install.md note

Add a short subsection to `docs/install.md` (camera-troubleshooting
area, if there's one — otherwise create one). Frame it as *what
SimpleNVR does for you*, not as homework:

> ### What if my camera's IP changes?
> SimpleNVR notices when a camera moves to a new address on your
> network and updates itself automatically. You shouldn't have to
> do anything — but if you ever see "Offline" for a camera that
> seems fine, opening the SimpleNVR app for a few minutes lets the
> next discovery scan catch up.

No mention of DHCP, MAC, or router reservations. The user doesn't
need to learn networking to use their NVR — that's the whole
product thesis.

## Files touched

- `backend/discovery/scanner.py` — MAC-fallback branch in the new-IP
  path. Calls `invalidate_arp_cache()` once per scan iteration where
  reconciliation is being checked.
- `backend/db.py` — `get_camera_by_mac`, `update_camera_ip`.
- `backend/recording/manager.py` — likely already wired to handle
  `camera_updated` events with new IP; verify the bounce path works
  when only the IP changed (no auth, no name, no override).
- `backend/go2rtc_client.py` — confirm `set_camera` overwrites in
  place when called with the same UUID + different upstream URL
  (it should; this is the same path the auth-update flow uses).
- `tests/test_mac_fallback.py` — table-driven tests for the
  MAC-match decision rule. Cases: no MAC in ARP → no match; MAC
  matches healthy camera → no match (false-positive guard); MAC
  matches offline camera → match; MAC matches multiple cameras
  (impossible in practice but worth a test that asserts we pick
  none rather than guess).
- `docs/install.md` — the user-facing note above.
- `docs/architecture.md` — once shipped, add a paragraph to the
  discovery section explaining the MAC-fallback identity rule
  and why it's gated to currently-offline cameras.

## Out of scope (deliberate)

- mDNS / Bonjour / SSDP-based identity tracking — overkill for the
  cameras we target, would add new dependencies.
- Re-discovery on user demand (a "scan now" button) — the periodic
  scanner already covers this, and adding a button violates "zero
  knobs" until we have evidence the auto-cadence is too slow.
- Surfacing IP changes in the UI as an event — the camera just stays
  online. Logging the move in the backend is enough for diagnosis.

---

## Resume prompt for fresh session

Copy/paste into a fresh Claude session to pick up this work:

> **Task: MAC-fallback IP reconciliation in the discovery scanner.**
>
> Background: `mac-fallback-ip-change-plan.md` at the repo root has
> the full design. Read it first.
>
> Currently, when a camera's DHCP lease changes, SimpleNVR treats the
> new IP as a brand-new camera and the old IP record stays
> permanently offline. The fix is MAC-based reconciliation in the
> scanner: a previously-unknown IP whose ARP MAC matches an existing
> camera that's currently offline → update the camera's IP in place
> rather than onboarding a duplicate.
>
> Implementation order:
>
> 1. Add `get_camera_by_mac` and `update_camera_ip` in `backend/db.py`.
>    `update_camera_ip` must re-derive `authed_uri` and `substream_uri`.
> 2. Add the MAC-fallback branch in `backend/discovery/scanner.py`,
>    gated to cameras whose health is `offline` or `record_failing`
>    (false-positive guard).
> 3. Verify the `camera_updated` event path bounces the recorder
>    correctly when only the IP changed (no auth/name/override
>    delta). The "Settings propagation invariant" in
>    `docs/architecture.md` is the relevant load-bearing notes.
> 4. Add `tests/test_mac_fallback.py` — table-driven decision-rule
>    tests as described in the plan doc.
> 5. Add the user-facing note to `docs/install.md` and the
>    architecture paragraph to `docs/architecture.md`.
> 6. Live-test by triggering a DHCP rebind on one of the cameras
>    (`sudo arp -d <ip>` won't reproduce it — needs an actual lease
>    change, easiest is power-cycling the camera with a short DHCP
>    lease time on the router). Verify: old IP doesn't get a
>    duplicate row, the camera comes back online at the new IP, the
>    UI Camera Setup view shows the new IP, and recording resumes
>    on the same go2rtc loopback UUID.
>
> Once shipped, mark this plan doc done in `CLAUDE.md`'s plan-doc
> index and move the description into `docs/architecture.md`.
>
> Tests: `PYTHONPATH=. pytest tests/ -q --ignore=tests/test_classifier_phase25.py --ignore=tests/test_motion_tracker.py` (those two have pre-existing stale imports).
