# Changes

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
