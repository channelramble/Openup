# Changes

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
