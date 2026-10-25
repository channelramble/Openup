# Changes

## 2.3.12

- Fix the iPhone 3G timer reset failing at `Error: Ramdisk command failed (127): /bin/sync`. The Legacy iOS Kit 3.1.3 SSH ramdisk is a minimal environment (`PATH=/usr/bin:/bin:/usr/sbin:/sbin`) that ships **no `sync` and no `cp`** — the device transaction had assumed a fuller Darwin toolset. `device.sync()` now resolves a `sync` binary and runs it where one exists (full iOS, the 4.x recovery ramdisk), so a genuine sync failure still rolls the transaction back, and degrades to a no-op when none exists — durability is then completed by the clean reboot (`reboot(2)` flushes mounted volumes) and crash-recovery stays state-based via the transaction manifest. `device.stage()` now writes the staged replacement directly over SFTP instead of shelling out to `cp` (the copied bytes were overwritten anyway, and the commit still archives the untouched original by renaming the live file aside). This also unblocks restore/finalize, which hit the same missing `/bin/sync`.
- **Verified on a physical iPhone 3G for the first time.** On an Apple Silicon Mac (M4 Pro) reaching the 30-pin phone through a USB-C hub, the full timer-reset flow — prepare → the Legacy iOS Kit 3.1.3 ramdisk → reset — completed: the SpringBoard lockout plist was committed and read-back verified, and an independent read confirmed `SBDeviceLockBlocked=false`, `SBDeviceLockFailedAttempts=0`, the cached timer-state keys removed, no wipe flags, and every other preference preserved. The matching recovery path (restore of an interrupted transaction) was exercised on the device too. On-screen timer clearance is confirmed only after the phone reboots.

All 378 source tests pass with both pinned script fixtures. No transaction ordering, backup, identity-check, or boot logic changed; only the two missing-binary assumptions were removed.

## 2.3.11

- Fix the logged "Unsupported or unidentifiable hardware: None" failure on the iPhone 3G timer flow. After the Legacy iOS Kit 3.1.3 SSH ramdisk booted and Openup connected over USB SSH (the fingerprint printed), `device.inspect()` tried to read the model with `sysctl -n hw.machine`/`hw.model`, but that minimal restore ramdisk serves no `sysctl` (or `uname`), so every probe returned nothing and the model resolved to `None`, stopping the reset before it began. Detection now also tries `sysctl` on `PATH` and in `/usr/bin`, tries `uname -m`, and — when the ramdisk answers no kernel query at all — uses the live partition scheme to *confirm* an expected iPhone 3G. It matches *only* the genuine flat iPhone 3G layout (`/dev/disk0s1` + `/dev/disk0s2`, with `/dev/disk0s1s1`, `/dev/disk0s1s2` and `/dev/disk0s2s1` all absent), so the iOS 4 nested scheme (data at `/dev/disk0s2s1`, where `/dev/disk0s2` is only a container) and the iOS 5+ LwVM scheme are not mistaken for a 3G. Because that flat scheme is not unique to the 3G, the fallback fires only when the caller expects an iPhone 3G — `--model` on inspect/reset/prepare (the desktop passes the selected model), the saved transaction identity on restore/finalize, and on prepare the kit DFU/iBSS boot has already verified the real board and chip (`n82ap` / S5L8900); otherwise it fails closed. `inspect()` is the on-ramdisk gate, backed by the iOS-version gate and the HFS-mount checks.
- Make USB device handling work through USB-C docks and hubs on Apple Silicon Macs, where a 30-pin iPhone can only reach the port through an adapter. Such setups re-enumerate the phone routinely, which keeps its USB serial but reassigns usbmuxd's numeric DeviceID; the connection treated that as a device swap and failed with "USB device changed during connection". Device identity is now keyed on the stable serial — a changed DeviceID for the same serial follows the phone to its new DeviceID, while a different or missing serial is still rejected (the pinned ramdisk SSH host key remains the cryptographic anti-swap backstop). A brief re-enumeration drop during the SSH handshake is retried for a few seconds instead of failing, and the post-boot re-enumeration wait gets more headroom (30s) for slower docks.
- Tests: new checks reproduce the 3.1.3-ramdisk model failure and confirm recovery, confirm that a ramdisk with no kernel query and a non-3G layout still fails closed, cover hub re-enumeration reconnecting to the new DeviceID (and a genuine serial change still being rejected and the socket closed), and cover the handshake-time connection retry.

All 377 source tests pass with both pinned script fixtures. No transaction, boot-sequence, payload, or backend-script logic changed. The physical iPhone-3G-on-3.1.3-ramdisk recovery and real Apple Silicon + USB-C dock behavior were root-caused from the device log and are covered by tests, but were not re-run on hardware here (no phone was attached).

## 2.3.10

- Fix a 20-second stall introduced in 2.3.9. The post-boot USB wait was applied to every device action, but only "prepare" boots a ramdisk and needs to wait for the phone to re-register with usbmuxd. On inspect/reset/restore/finalize a genuinely absent phone now fails immediately again (as before 2.3.9) instead of polling for the full timeout — and no longer holds Apple's device agents paused during that wait. Only "prepare" still waits (up to 20s) after its boot.
- Let Openup's device-agent guardian be the sole owner of Apple's AMPDevices agents during a timer reset. The Legacy iOS Kit's own startup `killall -STOP` — combined with the `--no-finder` that suppresses its resume — could, in a narrow startup race, leave an agent the guardian had not yet claimed suspended after the run. The timer adapters now neutralise the kit's STOP/CONT, mirroring the recovery backend, so only the precise guardian suspends and restores those agents.
- Test and quality: the device-agent reservation test no longer spins for the real timeout, and a new regression test covers a cancelled restore/finalize confirmation.

Found by an adversarial multi-agent review of the 2.3.6→2.3.9 changes (five confirmed findings after independent verification). All 370 source tests pass. No device-operation logic changed.

## 2.3.9

- Wait for the phone to re-enumerate on USB before the timer-reset device step. The flow queried usbmuxd once with no retry right after the kit handed control back; a just-booted ramdisk can take a second or two to re-register (notably on Intel Macs over direct USB, as the kit's own iproxy tunnel tears down), so the single query could land in that gap and report "No matching USB iPhone". It now polls for up to 20 seconds with on-screen progress, and still reports the same clear error immediately for a genuinely absent device. This addresses the post-boot re-enumeration cause; a separate pre-boot detection cause (the kit's own first probe) is not changed here.
- Require a typed confirmation before a timer reset. Because the reset writes to the phone, the confirmation now asks you to type "Yes"; its button stays disabled until you do. Restore and finalize keep their simple Yes/Cancel prompt.
- Begin a UI cleanup: the main screen is framed as Step 1 (connect your phone) and Step 2 (choose what to do), and the confusing dual-dropdown labels are replaced with clearer wording and status. Every surface stays OLED black.

All 369 source tests pass, including a new check that a declined reset confirmation never starts a device write. The device operations, transaction engine, boot sequence and activation handling are unchanged. The Intel device-grab fix still needs on-device confirmation, and the UI cleanup is an early pass.

## 2.3.8

- Fix the timer reset failing to grab the iPhone on Intel Macs. The reset-timer flow now holds the macOS device-agent reservation — the same guardian the recovery flow already uses — across the whole operation: the Legacy iOS Kit ramdisk boot **and** Openup's own ramdisk SSH session. Apple's AMPDevices agents otherwise race to claim the phone and win on Intel, so the device could not be grabbed. The guardian restores exactly the agents it paused when the operation finishes, including on interruption or parent death.
- Pass `--no-finder` to Legacy iOS Kit on macOS so it does not resume those agents when it exits, removing the brief window in which they could grab the phone during the handoff from the kit to Openup's SSH session.

The reservation is a no-op off macOS, and it wraps `inspect`/`reset`/`restore`/`finalize` as well as `prepare`. The plist transaction, backups, boot sequence, identity checks and RAM-root helper are unchanged. A regression test asserts the timer-reset path enters the reservation around the backend boot. Physical Intel-Mac timer reset still needs on-device confirmation.

## 2.3.7

- Finish the timer-reset preparation without the interactive SSH Ramdisk Menu. Booting the Legacy iOS Kit ramdisk previously dropped into a "Select an option" / "Choose the next step" menu whose only way forward was choosing Exit, which read like a terminal step. The backend now returns automatically once the ramdisk is ready, and Openup's own reset resumes, so the whole timer workflow is driven by the GUI.
- Leave the phone in the ramdisk exactly as before (equivalent to choosing Exit); no device operation is auto-answered in a loop. The menu still works normally when the kit is run outside Openup.
- Apply this to every supported timer-reset model on the pinned kit, not only the iPhone 3G. Non-3G models now receive the same GUI prompt/menu handling (without the 3G-specific identity checks), so the whole workflow stays in the GUI. A user-supplied, non-pinned checkout is still run unchanged.
- Update the preparation guidance so it no longer tells the user to choose Exit at the SSH Ramdisk Menu.

This mirrors the 2.3.4 removal of the recovery flow's repeating menu, now applied to the timer-reset path. Regression tests exercise the adapted `menu_ramdisk` (it returns without a prompt under Openup automation and still runs the interactive menu when the flag is unset), the non-3G menu-only adaptation, and the model routing in `launch_backend`. The identity checks, boot sequence, plist reset, backups and RAM-root helper are unchanged. Physical timer clearance remains pending validation.

## 2.3.6

- Disable stdout buffering in the bundled iPhone 3G recovery program. Piping output through the final-result display wrapper could hold progress for minutes, then print it in a burst, making a working process appear stuck at OK.
- Preserve the final passcode block at the bottom of the phone screen, including leading zeros. The recovery algorithm, result files, boot sequence and RAM-root helper are unchanged.
- Rebuild and verify the ARMv6 program, with matching source, patch, checksums, signatures and provenance. Include it in all four desktop downloads and the built-in updater.

A host C regression test reproduces the old buffering and verifies immediate output through the actual display wrapper before process exit. Physical screen validation of this revision remains pending. The native output fix applies to the bundled 3G program; custom payloads and other models are unchanged.

## 2.3.5

- Make Update now the primary Updates action. It finds the latest compatible release, downloads and verifies the complete desktop package, and stages it in one background operation before offering Restart.
- Require no browser, GitHub sign-in, token, file selection or separate install confirmation. Keep prereleases enabled by default so Openup's current releases are included.
- Move the local installer, rollback, release links and optional token into Advanced update options. Keep cancellation, repeat-click protection, verification and the existing installation intact until activation.
- Replace obsolete private-repository errors with specific retry guidance for rejected optional tokens, request limits and unavailable downloads.
- Preserve the recovery script's exit status across its final terminal-color reset. With the GUI's `TERM=dumb`, that cosmetic command returned 1 after an otherwise completed handoff, producing a false failure on the reported Intel Mac. Genuine errors and exit cleanup remain intact.

The firmware, boot sequence, native recovery engine, bundled dependencies and platform compatibility are unchanged. Sending boot commands still does not verify ramdisk startup or a recovered value.

## 2.3.4

- Remove the host's recursive Show Passcode / Exit menu. Recovery already runs automatically on the iPhone; the host now finishes once after sending the recovery system, without reading result files or writing over the phone console.
- Start recovery directly from its GUI button without a redundant confirmation. DFU guidance and required host-password prompts remain in the GUI; the terminal stays optional and hidden.
- Finish the GUI with Check your iPhone and clear instructions to wait for its result. Keep failed and interrupted operations distinct from this handoff, without claiming a recovered passcode or verified ramdisk startup.

The native recovery engine, boot chain and final on-phone passcode display are unchanged. Regression tests exercise the actual adapted host main function with fake device operations and no menu input. Physical recovery remains untested for this revision.

## 2.3.3

- Finish on-device recovery output with a short passcode result below the key and file-saving diagnostics, so the value remains visible on small phone screens. Preserve leading zeros and distinguish an unset passcode, an unsuccessful search, and a failed run.
- Remove the trailing 3G completion-status message and keep its post-recovery hold quiet. The shared display script wraps the existing native executable without changing the recovery algorithm or result files.
- Install the display script whenever a recovery ramdisk is prepared, including when firmware components are cached. Missing display resources stop preparation.

The display behavior is covered by simulated shell and ramdisk-preparation tests. Physical screen validation remains pending; existing device and platform compatibility limits still apply.

## 2.3.2

- Use true black backgrounds across the workspace, sidebar, cards, inputs, tables, dialogs, and optional terminal. Keep neutral borders, visible focus states, and readable blue, green, and amber status accents.
- Improve invalid-backup text contrast and keep disabled controls readable on black.
- Update download instructions for the now-public repository; GitHub credentials are optional in the desktop updater.

The device boot, recovery, and transaction code is unchanged from 2.3.1.

## 2.3.1

- Use the physically tested iPhone 3G chain: 3.1.3 iBSS, then 4.2.1 iBEC before the 4.2.1 ramdisk and kernel. Keep the first-stage firmware cache separate from 4.2.1 components.
- Verify each uploaded bootloader's board and exact build through its console. Reject installed iBoot even when USB reports Recovery, and allow bounded console readiness during the second-stage transition.
- Support the ARMv6 ramdisk's OpenSSH 5.2 RSA host-key signature on the selected USB transport. Preserve modern algorithm preference, password authentication and host-key pinning without changing global SSH settings.
- Fix GetMasterBlock error 16 by updating a verified HFS RAM root directly through mount(2), avoiding the helper's redundant raw-device open. Skip writable roots and verify required updates. Rebuild and revalidate the bundled ARMv6 executable and matching source/provenance archives.

The connected 3G reached the diagnostic ramdisk with authenticated SSH and SFTP on an Apple Silicon MacBook. Its startup files were verified and only the RAM root and devfs were mounted. This test did not run passcode recovery or clear a lockout timer. See VERIFICATION.txt for the final validation record and platform limits.

## 2.3.0

- Rename the application, repository, launchers, release packages, and CLI to Openup. Preserve existing backup/cache locations and verified older installations for rollback. Third-party file names and attribution remain accurate.
- Require a matching iPhone 3G Recovery-mode response after iBSS before sending the ramdisk. Show the observed mode and a bounded countdown in the GUI; explicitly report when the phone stays in DFU.
- Stop the boot chain when a recovery command fails. Describe boot commands as sent, without claiming verified ramdisk startup.
- Fix a macOS USB-query cleanup race when a query exits just before its process group is signalled.
- Prevent an older saved updater selection from silently opening an old app when the new Openup is launched. Explicit rollback selections remain persistent.

The latest physical tests reproduce the iBSS handoff failure, including with an original signed image and a newer USB client. The phone remains in patched DFU. A direct USB adapter retest is still needed; this release does not claim successful ramdisk boot. Open the new Openup download once to migrate from 2.2.x; later updates use the built-in updater.

## 2.2.2

- Add a built-in installer for desktop release ZIPs and their matching SHA-256 files. Download through your signed-in browser, select the files in Updates, and restart without a terminal, GitHub CLI, token, or dependency installation. Direct authenticated downloads remain available in an expandable section.
- Show the running version in the window title and a permanent Updates shortcut. After installing, clearly distinguish the running version from the version waiting for restart.
- Verify local update archives, platform, clean source revision, complete file inventory, and staged contents before activation. Retain rollback and cancellation behavior for both update paths.
- Timestamp each operation in exported logs and show update-bootstrap failures directly on the Updates page.
- Exercise the Normal → Recovery → WTF transition in the adapted-backend tests alongside the stock-DFU entry path. This release retains the 2.2.1 countdown and Mac agent fixes; it does not add speculative firmware changes.

The new reports still came from 2.2.0. Read-only inspection found 2.2.0 and 2.2.1 open simultaneously on the MacBook. No successful physical recovery is established by these logs or the simulated transition tests.

## 2.2.1

- Run the 3G button countdown from monotonic deadlines with a single, bounded USB query in flight. USB disconnects and slow queries no longer extend each displayed second.
- Preserve the Linux backend's terminal session for cached sudo authorization; background queries cannot request a hidden password.
- Restore the Mac device-agent coordination omitted by the passcode adapter. A detached guardian pauses only the current user's three exact Apple system agent paths and resumes the same process identities when the backend exits or its parent dies. Already-stopped agents are left alone.
- Keep a recovery journal for interrupted guardian cleanup, and stop before launching the backend if the Mac session cannot be reserved.
- Clear the stale “Response sent” status when active guidance resumes. Terminal and manual input remain off by default.

The supplied log stops in stock DFU before upload. On the user's MacBook, all three agents were running; live normal-exit and forced-parent-death checks confirmed they are restored. This supports the host-interference diagnosis but does not establish a successful physical recovery. Final package and updater results are recorded with [v2.2.1](https://github.com/channelramble/Openup/releases/tag/v2.2.1).

## 2.1.0

- Simplify the desktop around a shared phone selector and two starting choices: reset a lockout timer when the passcode is known, or start passcode recovery.
- Guide timer reset through Prepare, Review & reset, and Done with one primary next action. Keep explicit confirmation before applying changes and require a fresh review after phone or settings changes.
- Move optional firmware, custom tools, and connection settings into collapsed Advanced sections. Start recovery directly, downloading its backend when needed.
- Make Activity expandable and open it for operations and errors. Show specific progress and result messages, and lock phone/settings controls during an operation.
- Retain the existing recovery backends, bundled ARMv6 tools, backup protections, and update verification.

This release changes the desktop workflow. Final test, platform-package, and updater results are recorded with [v2.1.0](https://github.com/channelramble/Openup/releases/tag/v2.1.0). Physical iPhone recovery remains unverified.

## 2.0.2

- Fix iPhone 3G timer-ramdisk preparation selecting original-iPhone `m68ap` firmware when WTF mode reports a generic identity. Preserve the selected `n82ap` profile, then verify the real chip, board, and patched DFU state before sending iBSS.
- Apply that compatibility fix only to the reviewed Legacy iOS Kit script, using a temporary adapter that leaves the original checkout intact and honors the no-update flag. Check USB command failures, allow 15 seconds on macOS for the post-iBSS Recovery transition while retaining Linux's 24 seconds, and report the last readable USB state on timeout.
- Fix passcode recovery's first WTF download: the pinned downloader discards directory components in its output argument. Download into a staging directory, validate the result, and publish it into the cache before patching. Cover empty caches, spaces in paths, cache reuse, and failures.
- Preserve exported session output independently of the 2,500-line display. Keep up to 16 MiB in memory, include app/action/model/platform context, and explicitly mark omitted output if the limit is reached. Clear removes both the display and export transcript.

The original logs exposed gaps in the earlier offline checks. The new regression tests exercise these failing branches; a download-only check also uses the actual pinned downloader and Apple firmware endpoint. Physical 3G boot and recovery remain unverified. Final package and updater verification is recorded with [v2.0.2](https://github.com/channelramble/Openup/releases/tag/v2.0.2).

## 2.0.1

- Add an Intel Mac desktop archive targeting macOS 12 Monterey, including Python 3.13.16, Qt 6.9.3, and cryptography 48.0.1.
- Use Qt 6.9.3 on Apple Silicon as well; retain the macOS 14 minimum and cryptography 50.0.2 on that target.
- Inspect every Mac native binary's architecture, deployment version, and hardcoded library dependencies before signing. Omit unused Qt SQL drivers that require external database libraries.
- Forward command-line arguments from the native Mac launcher, including `--self-test` and `--original`.
- Include reproducibly built iPhone 3G ARMv6 recovery executables, their source/build recipe, provenance, and original license notices. Verify the bundled archive and extract it automatically into a private temporary directory; preserve its legacy code signatures.
- Require the matching 4.2.1/8C148 ramdisk and kernel for 3G passcode recovery, with input-hash verification and all seven expected kernel patch patterns. Keep timer-reset preparation on its separate 3.1.3/7E18 profile.
- Validate exact iBSS/iBEC hashes and RAM-disk boot arguments. Use managed startup with a bundled native wait helper, no automatic filesystem repair or read/write data mounts, and stop on preparation failures. Verify the complete preparation pipeline offline.

The Intel development build passed its six-page GUI self-test under Rosetta on macOS 27. Actual Monterey hardware, native Windows execution, and physical iPhone recovery remain untested. The following entries describe earlier releases; the ARMv6 availability and Mac compatibility changes above supersede their corresponding limitations. Release-specific verification is recorded with [v2.0.1](https://github.com/channelramble/Openup/releases/tag/v2.0.1).

## 2.0.0

- Replace the terminal-first experience with a Qt desktop workspace: devices, timer reset, recovery backend, backups, updates, and diagnostics.
- Ship platform archives containing Python, Qt, SSH libraries, native launchers, and package dependencies; opening the desktop app never invokes pip.
- Include a Linux Python/SSH runtime inside the Windows archive for WSL operations.
- Run device operations in separate terminal processes with live output and interactive replies; preserve the existing transaction engine.
- Add full desktop package updates, direct private GitHub authentication, platform/digest/inventory checks, staged activation, cancellation, and rollback.
- Keep credentials in memory and session logs unsaved unless explicitly exported.
- Add pinned runtime/wheel build inputs, native launcher source, and desktop regression tests.

USB drivers, WSL, and external backend system tools remain platform requirements. Hardware validation and compatible 3G ARMv6 recovery binaries remain outstanding.

## 1.2.0

- Integrate a pinned 32bit-Bruteforce-Passcode checkout without bundling third-party executables.
- Correct fallback boot-image names; cross-check recovery chip/board IDs and Mach-O architectures.
- Add an explicit iPhone 3G/iOS 4 timer-reset route, matching 7E18 boot profile, local IPSW validation, and old partition layout support.
- Reject upstream ARMv7 executables for the ARMv6 3G; accept separately supplied compatible ARMv6 builds.
- Add native Linux preparation, Windows USB SSH support, three platform launchers, and a Windows-to-WSL bridge for Unix ramdisk tools.
- Add authenticated GitHub update checks, bounded archive and checksum validation, staged activation, and rollback.
- Keep separate Python environments per app version.
- Extend CI configuration to Windows and add compatibility/update regression tests.

Physical device operations, native Windows execution, and WSL USB passthrough remain unverified. The upstream ARMv6 executable gap remains a limitation for 3G passcode recovery.

## 1.1.0

Published under the requested project name, **Openup**. The implemented function remains a one-time timer-state reset for eligible legacy iPhones.

- Replace multipart device-lock creation with an atomic owner-bearing link; support recovery of version 1.0 locks.
- Treat lost lock acknowledgments as requiring recovery.
- Preserve a verified commit when cleanup fails; add `finalize` for interrupted reset/restore cleanup.
- Sync staged data and archived originals before publishing replacements.
- Check the USB identity before and after tunnel establishment.
- Bound SSH authentication, channel requests, SFTP transfers, commands, and backend downloads.
- Reject malformed USB data, invalid identities, altered backup transformations, symlinks, special files, and read-only data volumes.
- Keep backups, host keys, and backend checkouts in the user's private state directory.
- Check the full dependency lock and native imports at launch.
- Add reproducible ZIPs with source revision and checksum records.
- Add CI on macOS and Linux and expand failure-injection coverage.

Physical iPhone validation remains pending.

## 1.0.0

Initial local implementation, named iPhone Timeout Reset. Included legacy compatibility gates, an SSH ramdisk backend wrapper, plist reset, backups, rollback, and 41 automated tests.
