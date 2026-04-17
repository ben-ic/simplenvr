# Installing SimpleNVR

The current SimpleNVR builds are not yet code-signed or notarized. Code signing is on the roadmap before the first public release. Until then, Windows and macOS will warn you on first launch because the app is from an "unidentified developer." That warning is the OS's default treatment of any unsigned binary; it does **not** indicate a problem with SimpleNVR. This document walks you through the one-time bypass each OS requires.

## What's in the installer

The installer is self-contained. Everything SimpleNVR needs to record your cameras and label what it sees is bundled inside the download:

- **FFmpeg** for recording and segment writing
- **go2rtc** for RTSP fan-out to recorder and live preview
- **D-FINE** object detector for person / vehicle / animal labels
- **YAMNet** audio classifier for glass-break, siren, bark, and other sounds that matter
- The full Python backend and the Tauri shell

There are **no separate downloads** after install, no model fetches, no "first-run data pack." You install the app, give it your camera passwords, and it starts working.

---

## Windows

1. Download the `.zip` from the project's download page and extract it.
2. Double-click to run. Windows SmartScreen will probably show **"Windows protected your PC — Microsoft Defender SmartScreen prevented an unrecognized app from starting."**
3. Click **More info** (the small text under the warning message).
4. Click **Run anyway** (the button that appears after "More info").
5. Click through the installer. SimpleNVR shows up in the Start menu.
6. On first launch, Windows may ask for **firewall permission** so SimpleNVR can reach cameras on the local network — allow it.

If SmartScreen doesn't offer "Run anyway" at all (rare, but some Group Policy settings hide it), right-click the `.exe` → **Properties** → check **Unblock** → **OK**, then double-click again.

---

## macOS

> **Heads up — the current build is Apple Silicon only.** The file you'll see on the download page is `SimpleNVR_0.1.0_aarch64.dmg`, which runs on Apple Silicon Macs (M1, M2, M3, M4). It will **not** run on Intel Macs — macOS will refuse to launch it, and Rosetta does not help (Rosetta translates Intel → Apple Silicon, not the other direction). Intel support is planned before the first public release, either as a separate `_x64.dmg` or as a single universal `.dmg`. If you have an Intel Mac, wait for one of those to appear on the download page.
>
> Not sure which chip your Mac has? Click the Apple menu in the top-left corner of the screen → **About This Mac**. If the "Chip" line says *"Apple M1"* (or M2, M3, M4), you're on Apple Silicon and the current build will work. If it says *"Intel Core i5"* / *"Intel Core i7"* / similar, hold off on downloading.

1. Download the `.zip` from the project's download page and extract it.
2. Drag **SimpleNVR** to the Applications folder.
3. Double-click SimpleNVR. The first launch will be blocked with **"SimpleNVR cannot be opened because Apple cannot check it for malicious software"** (or, on recent macOS versions, a similar "cannot verify the developer" dialog).
4. **Click Done** on that dialog — there's no "Open anyway" button in the dialog itself anymore; you have to approve the app from System Settings:
5. Open **System Settings → Privacy & Security**. Scroll to the **Security** section at the bottom. You'll see *"SimpleNVR was blocked to protect your Mac."*
6. Click **Open Anyway**. Confirm with your Mac password or Touch ID when prompted.
7. Double-click SimpleNVR again. This time, click **Open** in the confirmation dialog.
8. SimpleNVR will ask for **Local Network** permission on first launch — grant it so the app can discover your cameras.

**Alternative (faster, Terminal):** instead of clicking through System Settings, you can strip the quarantine flag directly. Open Terminal and run:

```bash
xattr -dr com.apple.quarantine /Applications/SimpleNVR.app
```

After that, SimpleNVR launches normally from Launchpad or Applications — no warning, no settings trip. This works on every version of macOS, but you have to be comfortable using Terminal.

---

## Linux

The Linux build is an AppImage — a self-contained executable that bundles all dependencies (including WebKitGTK). No system packages to install.

1. Download the `.zip` from the project's download page and extract it.
2. Make the AppImage executable:

```bash
chmod +x SimpleNVR_*.AppImage
```

3. Run it:

```bash
./SimpleNVR_*.AppImage
```

If your system doesn't have FUSE installed (required for AppImages), install it first:

```bash
sudo apt install libfuse2   # Debian/Ubuntu
```

---

## After installation

### What if my camera's IP changes?

SimpleNVR notices when a camera moves to a new address on your network and updates itself automatically. You shouldn't have to do anything — but if you ever see **"Offline"** for a camera that seems fine, opening the SimpleNVR app for a few minutes lets the next discovery scan catch up. No need to re-enter your camera's password or delete and re-add it.
