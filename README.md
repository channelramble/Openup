# Bruteforce Legacy iOS

[![Test and package](https://github.com/channelramble/Bruteforce-Legacy-iOS/actions/workflows/ci.yml/badge.svg)](https://github.com/channelramble/Bruteforce-Legacy-iOS/actions/workflows/ci.yml)

A macOS command-line tool that automates a **one-time passcode timer reset on legacy 32-bit iPhones** through an offline SSH ramdisk. Version **1.1.0** handles USB transport, plist editing, backups, verification, and transaction recovery. Cyberduck and a separate iproxy process are unnecessary.

**Hardware validation is pending.** The host software passes automated and simulated SSH/SFTP integration tests. No physical iPhone was available during development. Clearing the on-screen timer is not a verified result of this release.

The correct passcode remains required. Passcode guessing is not implemented. This release also excludes Activation Lock removal, setup.app removal, and computer pairing/Trust timeouts.

## Run

Download the ZIP from [Releases](https://github.com/channelramble/Bruteforce-Legacy-iOS/releases), then extract the whole folder onto the Mac physically connected to the phone. Python 3.9 or newer is required.

Double-click `Launch.command`, or open Terminal in the extracted folder:

```sh
./Launch.command
```

If the executable bit was lost during transfer:

```sh
bash Launch.command
```

First launch downloads the pinned Python dependencies into this folder's `.venv`. Every launch checks all locked versions and the native SSH dependencies. The menu offers inspection, reset, ramdisk preparation, recovery, and interrupted-cleanup finalization.

To start with pwnDFU/ramdisk preparation and then apply the reset:

```sh
./Launch.command prepare --apply
```

This downloads an official Legacy iOS Kit checkout at the revision recorded in `RESEARCH.md`, then starts its SSH-ramdisk workflow. An existing installation can be selected with:

```sh
./Launch.command prepare --kit "/path/to/Legacy-iOS-Kit" --apply
```

Connect only one phone for the backend's DFU preparation; `--udid` selects the device after ramdisk boot. Follow the backend's dependency and physical DFU prompts. When it reaches the **SSH Ramdisk Menu**, select **Exit**, keeping the phone in the ramdisk. The reset resumes automatically. Do not select a restore or erase operation.

The pwnDFU exploit and ramdisk builder belong to Legacy iOS Kit; they are not newly implemented here. First-time setup, physical DFU button presses, and backend menus remain interactive. The plist stage is automated. [Upstream ramdisk documentation](https://github.com/LukeZGD/Legacy-iOS-Kit/wiki/SSH-Ramdisk)

## Already in a ramdisk

Inspect first:

```sh
./Launch.command inspect --mount
```

Reset:

```sh
./Launch.command reset --mount --apply
```

Optionally request reboot after the files have been read back and verified:

```sh
./Launch.command reset --mount --apply --reboot
```

Without `--apply`, `reset` previews the proposed file changes. `--mount` can mount the system/data volumes, and `prepare` boots a ramdisk even without `--apply`; neither flag means the whole command is side-effect-free.

Use the correct passcode after reboot. Incorrect attempts can produce new delays, and any existing erase-after-failed-attempts setting remains in effect.

## Compatibility gate

These are the versions the program **accepts for inspection**, not a hardware-tested support matrix:

| iPhone | Product types | Accepted installed iOS |
|---|---|---|
| 4 | iPhone3,1 / 3,2 / 3,3 | 6.0–7.1.2 |
| 4S | iPhone4,1 | 6.0–9.3.6 |
| 5 | iPhone5,1 / 5,2 | 6.0–10.3.4 |
| 5c | iPhone5,3 / 5,4 | 7.0–10.3.3 |

Other models and versions are rejected. Each accepted phone still needs readable legacy HFS system/data partitions, recognized plist fields, an offline ramdisk, SFTP, and the usual Darwin system utilities. Filesystem layout changes, inaccessible encryption keys, unsupported ramdisk binaries, or read-only storage can prevent the operation.

iPhone 4S uses A5. Entering pwnDFU generally requires additional hardware; a software wrapper cannot remove that requirement. See the [upstream A5 guide](https://github.com/LukeZGD/Legacy-iOS-Kit/wiki/checkm8-a5).

Modern passcode delays are enforced by the Secure Enclave. This tool excludes iPhone 5s and newer. [Apple Platform Security](https://support.apple.com/guide/security/passcodes-and-passwords-sec20230a10d/web)

## Exact changes

The installed OS version is read from `/mnt1/System/Library/CoreServices/SystemVersion.plist`, not from the ramdisk's OS.

For `/mnt2/mobile/Library/Preferences/com.apple.springboard.plist`:

- Set `SBDeviceLockFailedAttempts` to integer `0`.
- Set `SBDeviceLockBlocked` to boolean `false`.
- Remove the known timer/cache fields `SBDeviceLockBlockTimeIntervalSinceReferenceDate`, `SBDeviceLockBlockStateGeneration`, and `SBDeviceLockCachedVCCaps`.
- Preserve other values and the original binary/XML plist format.

If `/mnt2/mobile/Library/SpringBoard/LockoutStateJournal.plist` exists, archive it alongside the original preferences so iOS can recreate it. The tool does not broadly delete preferences, temporary files, activation records, or user data. A pending `SBDeviceWipe` request stops the operation.

This narrower one-time reset is an implementation choice. Community procedures often change more fields and set negative counters; their reported outcomes do not validate this implementation. See `RESEARCH.md`.

## Backup and recovery

Every transaction saves private local copies under `~/Library/Application Support/Bruteforce Legacy iOS/backups/<timestamp>-<transaction>/`, with a checksum manifest. The exact path is printed before device changes begin. Original files also remain on the phone as hidden `.itr-<transaction>-<filename>.original` files. Keep both sets.

Changes are staged and synced before commit. The original is archived and synced before its replacement is published. Each individual rename is atomic; the two-file transaction as a whole is not. On a pre-commit error or Ctrl-C, the tool attempts rollback. A cable disconnect, power loss, or forced process termination may require recovery.

While the phone is still in its ramdisk:

```sh
./Launch.command restore "/path/to/backups/transaction-folder" --mount --apply
```

Recovery requires the same USB identifier, model, installed version, and build. It checks the saved checksums and refuses to overwrite unexpected new contents. Prefer recovery **before rebooting**; after iOS runs, its files may change and recovery can refuse. If connectivity is lost during a write, keep the phone in its ramdisk and reconnect before running recovery.

If the program says the files were committed and verified but lock cleanup failed, finish cleanup without undoing the reset:

```sh
./Launch.command finalize "/path/to/backups/transaction-folder" --mount --apply
```

`finalize` rechecks the completed transaction before releasing its lock. It also handles cleanup interrupted after a completed restore. A failed commit still requires `restore`. Do not remove transaction locks by hand.

Version 1.1 uses an atomic device lock whose owner is stored in the link itself, avoiding a separate owner-file write. Version 1.0 backup manifests and intact directory locks remain recoverable.

Backup originals on the phone retain their original inodes. Staged preferences use `cp -p`, and permissions and ownership are verified after writing. Preservation of every iOS-specific extended attribute/protection class has not been validated on hardware.

## Useful commands

```sh
./Launch.command doctor
./Launch.command devices
./Launch.command reset --udid YOUR_USB_IDENTIFIER --mount --apply
./Launch.command reset --help
```

`devices` uses usbmuxd and excludes network devices. It will not list a phone that is only in DFU/recovery. Multiple USB devices require explicit selection with `--udid`.

The default SSH credential is the ramdisk's `root` / `alpine`, not the phone's passcode. Use `--ask-password` for a custom ramdisk SSH password. Passwords are not saved.

The first SSH host key is trusted over the selected local USB connection and saved in the state directory. If a rebuilt ramdisk changes its key, inspect the device selection and use `--trust-new-key` once. This does not alter system SSH settings.

The default macOS state directory is `~/Library/Application Support/Bruteforce Legacy iOS`. Backups and downloaded backends live there, outside the source/install directory. Set `--state-dir` to use another private directory, including the previous `~/Library/Application Support/iPhone Timeout Reset` directory. `--backups` and `--kit` can override their individual locations.

The program never runs filesystem repair automatically. Read-only data volumes are accepted for inspection and rejected for writes. If volumes cannot mount, stop and investigate disk/ramdisk compatibility before trying again.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The suite covers plist formats and types, compatibility checks, corruption, interrupted commits, rollback, wrong-device recovery, changed files, symlinks, USB packet framing, multi-device selection, SSH host-key changes, and a real SSH/SFTP protocol round trip against a simulated filesystem.

`VERIFICATION.txt` records the checks performed. The simulated server is not an iPhone emulator.

CI runs on macOS with Python 3.9 and 3.13 and Linux with Python 3.13. Linux CI validates host logic and transport simulations; the integrated device-preparation workflow remains macOS-only.

For a regular Python environment, `python3 -m pip install .` exposes `bruteforce-legacy-ios`; the previous `iphone-timeout-reset` command remains an alias.

To build the source ZIP:

```sh
python3 build_release.py
```

The archive in `dist/` contains only explicitly selected source, documentation, and tests. It includes file checksums, a `SOURCE.json` revision record, and an executable launcher. Runtime data, downloaded firmware, dependencies, and backups are excluded.
