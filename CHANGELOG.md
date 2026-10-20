# Changes

## 2.1.0

- Simplify the desktop around a shared phone selector and two starting choices: reset a lockout timer when the passcode is known, or start passcode recovery.
- Guide timer reset through Prepare, Review & reset, and Done with one primary next action. Keep explicit confirmation before applying changes and require a fresh review after phone or settings changes.
- Move optional firmware, custom tools, and connection settings into collapsed Advanced sections. Start recovery directly, downloading its backend when needed.
- Make Activity expandable and open it for operations and errors. Show specific progress and result messages, and lock phone/settings controls during an operation.
- Retain the existing recovery backends, bundled ARMv6 tools, backup protections, and update verification.

This release changes the desktop workflow. Final test, platform-package, and updater results are recorded with [v2.1.0](https://github.com/channelramble/Bruteforce-Legacy-iOS/releases/tag/v2.1.0). Physical iPhone recovery remains unverified.

## 2.0.2

- Fix iPhone 3G timer-ramdisk preparation selecting original-iPhone `m68ap` firmware when WTF mode reports a generic identity. Preserve the selected `n82ap` profile, then verify the real chip, board, and patched DFU state before sending iBSS.
- Apply that compatibility fix only to the reviewed Legacy iOS Kit script, using a temporary adapter that leaves the original checkout intact and honors the no-update flag. Check USB command failures, allow 15 seconds on macOS for the post-iBSS Recovery transition while retaining Linux's 24 seconds, and report the last readable USB state on timeout.
- Fix passcode recovery's first WTF download: the pinned downloader discards directory components in its output argument. Download into a staging directory, validate the result, and publish it into the cache before patching. Cover empty caches, spaces in paths, cache reuse, and failures.
- Preserve exported session output independently of the 2,500-line display. Keep up to 16 MiB in memory, include app/action/model/platform context, and explicitly mark omitted output if the limit is reached. Clear removes both the display and export transcript.

The original logs exposed gaps in the earlier offline checks. The new regression tests exercise these failing branches; a download-only check also uses the actual pinned downloader and Apple firmware endpoint. Physical 3G boot and recovery remain unverified. Final package and updater verification is recorded with [v2.0.2](https://github.com/channelramble/Bruteforce-Legacy-iOS/releases/tag/v2.0.2).

## 2.0.1

- Add an Intel Mac desktop archive targeting macOS 12 Monterey, including Python 3.13.16, Qt 6.9.3, and cryptography 48.0.1.
- Use Qt 6.9.3 on Apple Silicon as well; retain the macOS 14 minimum and cryptography 50.0.2 on that target.
- Inspect every Mac native binary's architecture, deployment version, and hardcoded library dependencies before signing. Omit unused Qt SQL drivers that require external database libraries.
- Forward command-line arguments from the native Mac launcher, including `--self-test` and `--original`.
- Include reproducibly built iPhone 3G ARMv6 recovery executables, their source/build recipe, provenance, and original license notices. Verify the bundled archive and extract it automatically into a private temporary directory; preserve its legacy code signatures.
- Require the matching 4.2.1/8C148 ramdisk and kernel for 3G passcode recovery, with input-hash verification and all seven expected kernel patch patterns. Keep timer-reset preparation on its separate 3.1.3/7E18 profile.
- Validate exact iBSS/iBEC hashes and RAM-disk boot arguments. Use managed startup with a bundled native wait helper, no automatic filesystem repair or read/write data mounts, and stop on preparation failures. Verify the complete preparation pipeline offline.

The Intel development build passed its six-page GUI self-test under Rosetta on macOS 27. Actual Monterey hardware, native Windows execution, and physical iPhone recovery remain untested. The following entries describe earlier releases; the ARMv6 availability and Mac compatibility changes above supersede their corresponding limitations. Release-specific verification is recorded with [v2.0.1](https://github.com/channelramble/Bruteforce-Legacy-iOS/releases/tag/v2.0.1).

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

Published under the requested project name, **Bruteforce Legacy iOS**. The implemented function remains a one-time timer-state reset for eligible legacy iPhones.

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
