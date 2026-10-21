# Changes

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
