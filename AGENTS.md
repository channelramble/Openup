# Openup development handoff

This file applies to the whole repository. Keep it current when architecture,
release procedures, or verified hardware behavior changes. The snapshot below
is dated **2026-10-25**; inspect the checkout and connected devices before
assuming that a historical state still applies.

## Start here

- Project: **Openup**, a dark-mode desktop application for legacy iPhone recovery.
- Repository: <https://github.com/channelramble/Openup> (public).
- Current app version: **2.3.13**, with one-click desktop updates.
- Verified device baseline: **2.3.1**, a prerelease built from
  `d6a5117a25038b850a56cea2c2f89b151930c104`. Version **2.3.12** is the first to
  complete a physical iPhone 3G timer reset (SpringBoard lockout plist committed
  and read-back verified live on the device; see VERIFICATION.txt).
- Release: <https://github.com/channelramble/Openup/releases/tag/v2.3.13>.
- Read `README.md`, the newest section of `VERIFICATION.txt`, `CHANGELOG.md`,
  and `THIRD_PARTY.md` before modifying device operations or packaging.
- `RESEARCH.md` and older verification sections contain historical hypotheses
  and failed candidates. The newest verified results supersede those claims.
- Check `git status --short` and preserve existing user changes. Build release
  artifacts from a clean, committed checkout. Keep experiments in ignored
  `build/`, downloads in `.desktop-cache/`, and distributions in `dist/`.

## Latest continuation (2.3.13)

The preceding Openup Chat 2 ended during investigation of the **separate 3G
passcode-recovery boot**, after v2.3.12's successful timer-reset release. The
supplied recovery log verified iBSS and then failed while reading the iBEC
console; no ramdisk was sent. Later console reads in that chat were inconsistent.
Those observations do **not** establish USB hub data corruption, NAND failure,
or a successful complete recovery boot. Keep the underlying physical cause open
until a fresh device session proves it. `SRTG=N?A` in Openup diagnostics is
`early_dfu_summary` sanitizing the slash in `N/A`, not evidence of USB corruption.

Continue from `/Users/user/crosstalk-workspaces/chat-2026-10-25/Openup` on this
Mac. The old `/chat-2026-10-20/iphone-timeout-reset` checkout has a separate,
unpushed overview redesign at `1019fdf`; it was not included in this release.
Do not overwrite or silently merge it. No `CLAUDE.md` or `agents/claude.md` was
present in the current repository; this `AGENTS.md` is the shared handoff.

`recovery_timing.RecoveryProbe.bootloader_console()` is now nonblocking:
`None` means a console child is still in flight. Do not call `poll()` concurrently
with that read. The owner closes/reaps it at cancellation or the stage deadline.
Only completed zero-status output within the size limit is accepted. Keep the
30-second read / 90-second overall bounds, responsive countdown, exact stage /
board / build gate, and immediate rejection of a concrete wrong hardware ID.
Tests include real slow subprocesses, late/partial results, failed/oversized
output, mixed banners, and cleanup; shell integration uses the same public probe
contract. Its fake-clock stage wait explicitly uses `timeout=3`: each fixture
needs only one successful stage poll, while inheriting the real 90-second
default spawns 180 real helper processes and exceeds the harness's 30-second
subprocess cap under Rosetta. Production remains 90 seconds. Do not restore
the prior patch's catch-all identity retry.

A fresh read-only check in this continuation found a connected 3G reporting
`iBoot for n82ap / iBoot-931.71.16` in 0.6 seconds, with its console and USB
identifiers agreeing. It was not verified as iBEC, so no further component was
sent. The patched read-only stage wait also stopped at a three-second test
deadline without a success event, with responsive progress and no remaining
console child. Full testing needs a fresh physical button-entry sequence. Never loosen
the iBEC check to accept this installed-iBoot banner.

The iPhone 5/5c timer range ends at **10.2.1** because the device adapter handles
legacy HFS only. Apple's default filesystem changed to APFS at iOS 10.3. APFS
must be rejected explicitly; do not claim it works because model/version unit
tests pass, and do not add filesystem conversion/repair. A known model mismatch
must stop before mounting. The existing 4/4S ranges remain inspection gates,
with no new physical verification implied.

Source validation for 2.3.13: **391 tests passed, zero skips**, with both
hash-verified pinned script fixtures and Python 3.13.16. The desktop self-test
and dependency checks passed. Final package and CI results belong in the release
notes and the post-publication handoff below.

## Product requirements

- Keep default surfaces true OLED black (`#000000`) in both the stylesheet
  and native Qt palette, with readable borders, text, and focus/status accents.
- The normal workflow must work entirely through GUI controls. Keep the terminal
  and manual input hidden and disabled by default, including after failures.
  Manual input is a separate Advanced opt-in; closing the terminal disables it.
- Use on-screen steps, choices, masked password prompts, countdowns, and result
  cards. A successful process exit does not prove a recovered passcode or a
  running ramdisk. Report only what the operation actually verified.
- Preserve the shared phone selector, locked settings during operations, and
  timer workflow: Prepare, Review & reset, Done. Changing the selected device
  or settings invalidates the previous review.
- Long operations run outside the GUI process. Keep cancellation, cleanup,
  rollback, and close-window behavior intact.
- The recovery Start button is the user's instruction to proceed. Keep the
  host's recursive result-reading menu removed; never auto-answer it in a loop.
  The phone runs recovery automatically and displays the final value itself.
  After boot commands finish, the GUI shows Check your iPhone. That handoff
  does not prove ramdisk startup or a recovered value. Required DFU and host
  password prompts still use GUI controls. Timer-reset confirmations are separate.
- Timer-reset preparation boots the Legacy iOS Kit ramdisk and then returns
  without its interactive SSH Ramdisk Menu. `cli.launch_backend` sets
  `BL_IOS_SSHRD_AUTOEXIT=1`; `legacy_kit._disable_sshrd_menu` makes `menu_ramdisk`
  return immediately (equivalent to choosing Exit, leaving the phone in the
  ramdisk) when that flag is set. This is not a loop auto-answer: the menu is
  never entered. The stock interactive menu is preserved when the flag is unset.
  The edit is model-independent: iPhone1,2 uses `adapt_script` (full 3G hooks),
  other supported models use `adapt_menu_only`/`prepared_menu_script` (shared GUI
  prompt/menu handling, no 3G identity hooks), and a non-pinned custom checkout
  runs unchanged. Keep the whole timer workflow (Prepare, Review & reset, Done)
  driven by the GUI.
- The GUI worker uses `TERM=dumb`. Preserve the recovery script's main status
  across its final cosmetic `tput sgr0`; on macOS that reset returns 1 for a
  dumb terminal and previously made completed handoffs look failed. The adapter
  explicitly exits with the saved status and still runs normal EXIT cleanup.
  Test the actual script footer as well as its main function; never convert a
  real operation failure into success based only on handoff text.
- Desktop downloads include their Python, Qt, and SSH dependencies. Opening
  the app must not invoke pip or require a developer environment. USB drivers,
  backend host tools, firmware, WSL, and USB forwarding remain separate needs;
  do not describe those as bundled or universally automatic.
- Keep public branding **Openup**. Existing compatibility identifiers, state
  locations, and upstream interfaces must continue to work. Do not rename or
  copy live state directories merely to change their displayed name.
- This app does not implement Activation Lock removal or setup.app removal.
  Timer reset preserves the existing passcode. iPhone 5s and newer are excluded.

## Code map

| Area | Files and responsibilities |
| --- | --- |
| Entry points | `desktop.py`: isolated desktop, CLI worker, session, and updater dispatch. `bootstrap.py`, `Launch.command`, `Launch.sh`, `Launch.cmd`: source-edition startup. |
| GUI | `ios_timeout/gui.py`: Qt windows and controls. `desktop_model.py`, `gui_flow.py`: command construction and workflow state. |
| Worker communication | `desktop_session.py`: subprocess/terminal lifecycle. `desktop_protocol.py`, `backend_ui.py`: framed events, prompts, replies, and redaction. `desktop_results.py`: bounded result summaries. |
| Logs | `session_log.py`: in-memory export transcript, independent of terminal scrollback. |
| Device operations | `cli.py`: orchestration. `core.py`: compatibility, plist plans, backups, transactions, restore, finalize. `device.py`: remote filesystems, locks, SSH, and device identity. |
| USB and SSH | `usbmux.py`: USB-only transport and framing. `ssh_compat.py`: transport-local legacy RSA host signatures. |
| Recovery preparation | `upstream.py`: pinned recovery backend and temporary script adaptation. `legacy_kit.py`: separate timer-ramdisk adapter. `backend_support.py`, `firmware.py`: identity, executable, and firmware validation. |
| 3G support | `armv6_boot.py`, `armv6_kernel.py`, `armv6_payloads.py`, and `ios_timeout/data/`: checked boot components, kernel patches, payloads, and startup script. |
| Host integration | `host.py`: state and safe local files/locks. `macos_agents.py`: device-agent reservation and restoration. `recovery_timing.py`: monotonic countdowns and bounded USB observation. `wsl.py`: Windows bridge. |
| Updates | `desktop_updates.py`: complete desktop packages. `updates.py`: source-edition updates. These are distinct mechanisms. |
| Distribution | `build_desktop.py`, `desktop-build-lock.json`, `macos_compatibility.py`, `packaging/`: desktop runtimes, launchers, and native binary checks. `build_release.py`: source ZIP allowlist. |
| Payload source | `third_party/iphone-dataprotection/`: canonical C source, original source, build recipe, headers, patches, licenses, and validation records. |

## Data and operation invariants

- Preserve the transaction engine's backups, file metadata, binary/XML plist
  format, unrelated preferences, sync/read-back verification, device identity
  checks, locks, interrupted-operation journal, restore, and finalize behavior.
- The installed OS version comes from the mounted system partition, not the
  ramdisk's own OS. An accepted model/version is an inspection gate, not a
  hardware-tested support claim; see `core.py` and the README matrix.
- Never add automatic filesystem repair. Read-only data may be inspected but
  must be rejected for writes. A writable RAM root does not authorize a
  writable NAND mount or an on-device recovery operation.
- `prepare` can boot a ramdisk without `--apply`; `--mount` can also change mount
  state. Do not describe either flag as a completely side-effect-free preview.
- Resolve state through `host.state_directory()`. It preserves a preexisting
  legacy location where needed. Fresh locations are macOS Application Support
  `/Openup`, Windows local application data `/Openup`, and Linux
  `$XDG_STATE_HOME/openup` or `~/.local/state/openup`. WSL has separate state.
- Keep backups, host keys, backend caches, and update pointers out of app
  bundles. Keep bundles read-only; launch with isolated Python and no bytecode
  writes so execution does not invalidate checksums or signatures.
- Logs remain in memory until exported. Display scrollback is 2,500 lines;
  export retention is independently bounded at 16 MiB with an omission marker.
  Clear removes both. Replies are not appended by the app, but backend output
  can contain sensitive results. Never commit raw logs or device identifiers.
- Keep GitHub credentials in memory. Do not print tokens or place them in
  command arguments, release notes, files, or logs.

## Verified 3G boot behavior and known traps

The tested phone is `iPhone1,2` / `n82ap`, CPID `0x8900`, BDID `0x04`.
WTF mode can expose a generic original-iPhone identity. Preserve the selected
profile during preparation, then verify the real board and chip after the
shared transition and before sending board-specific components.

The physically verified diagnostic sequence is:

1. Reach WTF and then verified patched DFU, or reuse already verified patched
   DFU. Ordinary stock DFU requires fresh button entry. The expected patched
   signature is `iBoot-636.66.3x`.
2. Load the **3.1.3 / 7E18 iBSS**. Require its console identity:
   `iBSS for n82ap` and `iBoot-636.66.33`.
3. Load the **4.2.1 / 8C148 iBEC**, issue `go`, and require its console identity:
   `iBEC for n82ap` and `iBoot-931.71.16`.
4. Continue with the matching **4.2.1 ramdisk, device tree, and kernel**.
   Confirm startup through actual USB SSH, not a transfer return code alone.

Do not collapse these stages. The 4.2.1 iBSS stayed in DFU during testing.
Launching the 4.2.1 kernel directly from 3.1.3 iBSS returned to installed iBoot.
Recovery USB mode and even a matching build string can describe installed
iBoot; exact stage/board/build console gates are essential. Failed commands,
transfers, or ambiguous identities stop later uploads. Keep the 3.1.3
first-stage cache separate from the 4.2.1 components.

The **timer-reset preparation route is different**: Legacy iOS Kit selects its
3.1.3 / 7E18 ramdisk. That route was not physically revalidated by the 4.2.1
diagnostic. Installed iOS, first-stage firmware, and ramdisk version are distinct.

Other fixes that must remain intact:

- The pinned partial ZIP downloader discards directory components from an
  output argument. Download into a staging directory, validate, then publish
  into cache. Exercise empty caches and paths containing spaces.
- Countdowns use elapsed monotonic deadlines and one bounded USB query in
  flight. Blocking USB calls must not stretch each displayed second.
- Hold the host lock and Mac device-agent reservation across the entire boot,
  including re-enumeration and stage identity checks. Restore only the exact
  processes paused by this operation, including on interruption or parent death.
  Both flows use `macos_agents.recovery_agents`: the recovery flow via
  `upstream.run_backend`, and the timer flow via `cli.run` (it wraps
  `prepare`/`inspect`/`reset`/`restore`/`finalize` so Apple's AMPDevices agents
  cannot grab the phone during the kit boot or the ramdisk SSH session — they
  win that race on Intel Macs otherwise). On macOS `launch_backend` also passes
  `--no-finder` so the kit does not resume those agents on exit; the guardian
  owns restoration. The reservation is a no-op off macOS.
- Adapt only the reviewed backend revision and exact script hash, with exact
  replacement counts. Leave the original checkout unchanged. Recovery backend
  pin: `4949674b318767de83eb149e57bcc543a6453d6e`; Legacy iOS Kit pin:
  `23622f8fe72c09952089d5145a7b008cfcc90d9b`. Constants in the adapters and
  `cli.py` are authoritative for subsequent changes.
- Paramiko 5 removed the `ssh-rsa` SHA-1 host signature required by the ramdisk's
  OpenSSH 5.2. `ssh_compat.py` adds it only to the selected USB transport, after
  modern algorithms. Preserve host-key pinning, password authentication, and
  global SSH settings. Test actual SSH/SFTP handshakes and changed-key rejection.
- USB device identity is the usbmuxd SerialNumber (udid), not the ephemeral
  numeric DeviceID. `usbmux.tunnel` re-resolves the current DeviceID for the
  selected serial so a USB-C dock/hub re-enumeration (same serial, new DeviceID,
  the normal case for a 30-pin phone on an Apple Silicon Mac) is followed rather
  than rejected as a swap; a different or absent serial still raises.
  `device.USBDevice._open_tunnel` retries the usbmux connection for a few seconds
  to ride a single handshake-time re-enumeration bounce. The pinned ramdisk SSH
  host key is the cryptographic anti-swap backstop. Keep the serial-keyed identity
  and the host-key pin; do not reintroduce a DeviceID-equality check.
- `device.inspect(..., expected_model)` identifies the model from the booted
  ramdisk by sysctl (`/usr/sbin`, `/sbin`, `/usr/bin`, PATH) and `uname -m`. When
  the ramdisk answers no kernel query (the Legacy iOS Kit 3.1.3 timer ramdisk
  ships no sysctl/uname, which produced the logged "Unsupported or unidentifiable
  hardware: None"), `_flat_3g_partition_layout` CONFIRMS an expected iPhone 3G
  from the live partition scheme — it never guesses, because the flat scheme is
  not unique to the 3G (an iPod touch 2G shares it). The fallback fires only when
  `expected_model == "iPhone1,2"`: cli.run passes `--model` (the GUI sends it on
  inspect/reset/prepare; restore/finalize derive it from the saved identity), and
  on prepare the kit DFU/iBSS boot has already verified `n82ap` / S5L8900. Match
  ONLY the genuine flat 3G scheme — `/dev/disk0s1` + `/dev/disk0s2` present AND
  `/dev/disk0s1s1`, `/dev/disk0s1s2`, `/dev/disk0s2s1` all absent — per the
  project's own `mount_partitions.sh`/`util.c`: the iOS 4 nested scheme keeps
  `/dev/disk0s2` as a block container, so a bare `test -b` on it is not enough to
  tell it from the flat 3G. Backed by the iOS-version gate (`core.compatible`)
  and the HFS-mount checks. With no expected 3G, or any non-flat layout, inspect
  must fail closed. Do not restore the free-guess fallback.
- The 3.1.3 timer ramdisk is a minimal environment (`PATH=/usr/bin:/bin:/usr/sbin:/sbin`)
  with NO `sync`, `cp`, `ps`, `sysctl`, `uname`, or `umount`; it does have `mv`,
  `rm`, `ln`, `mkdir`, `dd`, `tar`, `cat`, `chmod`, `chown`, `launchctl`, `mount`,
  `mount_hfs`, `fsck_hfs`, `reboot_bak`. `device.stage` creates the staged file
  over SFTP (no `cp`); `device.sync` resolves a `sync` binary and runs it when
  present (so a real failure still rolls back) but is a no-op when none exists,
  with durability completed by the clean reboot (`reboot(2)` syncs) and
  state-based crash-recovery. Do not reintroduce bare `/bin/cp` or a mandatory
  `/bin/sync`; keep device ops limited to tools this ramdisk actually provides.
  The `ps`/`launchctl` SpringBoard check and `reboot_bak`/`reboot` fallback are
  already written this way.

### RAM-root startup and Error 16

`restored_external.c` previously called the shell mount helper. Both `mount /`
and `mount -uw /` could emit GetMasterBlock EBUSY (16) while returning success.
Skipping already writable roots was insufficient because fresh boot starts
read-only. The current implementation uses a checked `mount(2)` HFS update.

It requires an HFS root at `/`, `MNT_ROOTFS`, a block device at `/dev/md0`, and
matching `stat("/").st_dev` / `stat("/dev/md0").st_rdev`. XNU initially calls
the mount source `root_device`, so matching that source string to `/dev/md0`
is incorrect. Preserve existing HFS ownership, permissions, encoding, and
timezone; skip writable roots and verify the final flags after an update.

Use the pinned, unmodified `include/hfs/hfs_mount.h` layout. A failed or
ineffective root update stops startup. Diagnostic failure must remain visible
instead of falling through into stock launchctl and unrelated directory errors.

The final tested `restored_external` SHA-256 is
`daff3018878a7e8112bbfc2fedcc9ab7027942940affa680757f89569209d5ea`.
The phone's final photo showed a writable RAM root, OK, and SSH startup, with
no Error 16 visible. Authenticated SSH/SFTP verified that binary and mounts
containing only `/dev/md0` and devfs. The bounded wait helper passed its
one-second execution and invalid-delay rejection checks on the phone.

## Development and tests

The on-device `ios_timeout/data/recovery-display.sh` wraps the native
recovery engine at `/usr/bin/openup-recovery-engine`. It forwards output without
saving a transcript, then prints the result after all diagnostics. Only an
explicit reported value with a completed zero exit and no validation failure
can become the final recovered-passcode block. Preserve leading zeros; bare
numeric guesses are not results. Explicit no-passcode, failure, incomplete
output and no-result endings are distinct. The 3G startup holds quietly after
the result. The adapter installs the wrapper on every ramdisk preparation,
including cached firmware; no native payload rebuild is needed for shell-only
display changes. `tests/test_recovery_display.py` uses fake engine output, not
real recovery. The new output still needs physical screen validation. Version
2.3.6 rebuilds
the bundled 3G recovery program with stdout buffering disabled before any
initialization. The pipe otherwise holds progress and emits it in bursts.
`tests/test_recovery_streaming.py` compiles only the actual stdio helper in a
harmless host fixture: a control reproduces buffering and the fixed run delivers
progress through the real display wrapper before exit. Stdin and engine
arguments are preserved. Custom payloads and other models are unchanged.
The RAM-root and device-info executables remain byte-identical to 2.3.1; only
the recovery executable changes. Its exact console-output patch, original
source, manifests and validation records are included in the payload source bundle.

Source support starts at Python 3.9. Use Python **3.13** for the complete desktop
development environment. These are maintainer commands, not end-user setup:

```sh
python3.13 -m venv .desktop-venv
.desktop-venv/bin/python -m pip install -r requirements-build.txt
.desktop-venv/bin/python -m pip check
.desktop-venv/bin/python check_dependencies.py
QT_QPA_PLATFORM=offscreen .desktop-venv/bin/python -B desktop.py --self-test
QT_QPA_PLATFORM=offscreen .desktop-venv/bin/python -B -m unittest discover -s tests -v
```

The full shell integration suite requires the two separately downloaded,
hash-verified script fixtures used by `.github/workflows/ci.yml`. Set
`BL_IOS_UPSTREAM_SCRIPT` and `BL_IOS_LEGACY_SCRIPT` to their absolute local paths
before discovery. Their local filenames can be descriptive aliases. Without
the fixtures, tests skip; do not equate that result with a full release run.
Darwin C-helper tests require macOS headers and a compiler. Inspect skips on
every platform. On Windows use the virtual environment's `Scripts/python.exe`.

Choose tests appropriate to the change:

- Transactions/transport: `test_core`, `test_recovery`, `test_boundaries`,
  `test_compatibility`, `test_usbmux`, `test_ssh_integration`.
- Boot and payloads: `test_backend`, `test_upstream_integration`,
  `test_armv6_boot`, `test_armv6_kernel`, `test_armv6_payloads`,
  `test_armv6_root`, `test_armv6_wait`, `test_recovery_timing`,
  `test_recovery_display`, `test_recovery_streaming`,
  `test_macos_agents`.
- Desktop and updates: `test_gui`, `test_gui_flow`, `test_backend_ui`,
  `test_desktop`, `test_desktop_session`, `test_desktop_results`,
  `test_session_log`, `test_updates`, `test_brand_migration`,
  `test_distribution`, `test_macos_compatibility`.

For example, use `-m unittest tests.test_armv6_root -v` with the same Python.
Fixture-based USB tests simulate shell control flow; they do not boot a phone.

At release 2.3.1, **330 tests passed** in source and in each packaged Mac build.
Apple Silicon executed natively; the Intel package executed through Rosetta
on the MacBook. Both launchers and strict signatures passed. Hosted CI could
not start due to the account's billing/spending block; do not claim it passed.
The CI matrix is macOS/Python 3.9 and 3.13, Linux/Python 3.13, and
Windows/Python 3.13. Recheck the service state before attributing future failures.
CI resumed successfully for 2.3.2 through 2.3.5, including all four platform builds.
For 2.3.6, all 361 source tests passed; package and CI results are recorded in
the release notes. The new native console-output behavior needs phone validation.
For 2.3.7, all 364 source tests passed (the new tests run with
`BL_IOS_LEGACY_SCRIPT` set). The SSH-Ramdisk-Menu auto-exit still needs phone
validation. For 2.3.8, all 365 source tests passed; the new test asserts the
timer-reset path wraps the backend boot in the device-agent reservation. The
Intel-Mac device-grab fix still needs on-device confirmation. For 2.3.9, all 369
source tests passed. `usbmux.wait_for_device` adds a bounded post-boot USB
re-enumeration poll (cli.run) for the post-boot "No device found" cause; the
GUI reset now requires a typed "Yes" (`require_typed_confirmation`, used from
`start_operation`), and the overview starts a Step 1/Step 2 cleanup. The
pre-boot detection cause and the Intel device-grab outcome still need a device
log; the UI cleanup is an early pass. For 2.3.10, all 370 source tests passed: a
multi-agent adversarial review of 2.3.6→2.3.9 confirmed five findings. The
post-boot USB wait is now gated to `prepare` only (`wait_timeout = 20.0 if
args.action == "prepare" else 0.0` in cli.run) so other actions fail immediately
on an absent phone, and `legacy_kit._release_kit_agent_control` neutralises the
kit's own `killall -STOP/-CONT AMP` so the `recovery_agents` guardian is the sole
owner of the Mac device agents (mirrors upstream.adapt_script).
For 2.3.11, all 377 source tests passed with both pinned fixtures (suite ~79s).
It root-causes a physical Intel-Mac log: the 3G timer flow booted the kit 3.1.3
SSH ramdisk and connected over USB SSH, then stopped at `device.inspect` with
"Unsupported or unidentifiable hardware: None" because that bare ramdisk serves
no sysctl/uname. `device._probe_model` now adds more sysctl paths, `uname -m`,
and a live partition-scheme fallback that identifies the iPhone 3G
(`/dev/disk0s1`+`/dev/disk0s2`, no `/dev/disk0s1s1`). It also adds USB-C dock/hub
support for Apple Silicon: `usbmux.tunnel` keys identity on the serial and
follows a re-enumerated DeviceID instead of raising "USB device changed", and
`USBDevice._open_tunnel` retries a handshake-time re-enumeration bounce.
For 2.3.12, all 378 source tests passed with both pinned fixtures. A physical
session (2.3.11) on an Apple Silicon Mac got all the way to the reset write and
failed with "Ramdisk command failed (127): /bin/sync": the 3.1.3 ramdisk ships
no `sync` and no `cp`. `device.sync` now resolves/runs a `sync` binary when one
exists (real failures still roll back) and no-ops when none does; `device.stage`
creates the staged file over SFTP instead of `cp`. This release is THE FIRST to
complete a physical iPhone 3G reset: on the connected iPhone1,2 / iOS 4.2.1 in
the 3.1.3 ramdisk over a USB-C hub, inspect previewed, restore recovered the
interrupted 2.3.11 transaction, and reset --apply committed the lockout plist
and read-back verified it (independent SFTP read confirmed
SBDeviceLockBlocked=false, SBDeviceLockFailedAttempts=0, cached timer keys gone,
other prefs preserved). On-screen timer clearance after reboot and the separate
passcode-recovery (4.x bruteforce) flow remain unverified.
For 2.3.3, 344 tests passed in source and the native Apple Silicon package;
all-platform updater installation/rollback and a public download check passed.
The MacBook was unreachable, so that release was not run under Rosetta there.

## Payload maintenance

- Begin with `third_party/iphone-dataprotection/BUILD.txt`, `PROVENANCE.json`,
  `VALIDATION.json`, `SOURCE-MANIFEST.json`, and `NOTICE.txt`.
- Canonical source is under that directory's `source/`; preserved originals
  are under `original/`. Retain licenses and regenerate the exact source patch.
- The build needs macOS clang/codesign, a separately obtained iPhoneOS 4.2 SDK,
  and the pinned, patched cctools linker. Modern Apple ld cannot link this
  ARMv6 target. Follow the recorded recipe rather than changing binary headers.
- Required properties: ARMv6/ARM1176JZF-S, iOS 4.0 minimum, SDK 4.2,
  `LC_UNIXTHREAD`, SHA-1 primary plus SHA-256 alternate code signatures, and
  valid imports against the actual matching ramdisk libraries.
- Build twice and compare bytes. Update the payload ZIP, its JSON manifest,
  source ZIP, source manifest, patch, notices, provenance, and validation
  together. The files in `ios_timeout/data/` must match the canonical sources.
- Keep the managed wait helper available independently of custom payloads.
  Its `--sleep` mode accepts decimal 0 through 3600, handles interruptions, and
  returns before any hardware initialization. Missing/failing waits must not
  turn failure holds into busy loops.
- Do not distribute Apple firmware, SDKs, proprietary runtime libraries, or
  private device logs. Keep build inputs and diagnostics in ignored storage.

## Desktop builds, updates, and releases

| Target | Runtime baseline at 2.3.1 |
| --- | --- |
| `macos-x86_64` | macOS 12 Monterey minimum; Python 3.13.16, Qt 6.9.3, cryptography 48.0.1 |
| `macos-arm64` | macOS 14 minimum; Python 3.13.16, Qt 6.9.3, cryptography 50.0.2 |
| `windows-x86_64` | Bundled Windows runtime plus Linux runtime for WSL; Qt 6.12.0 |
| `linux-x86_64` | Ubuntu 22.04+ or equivalent desktop baseline; Qt 6.12.0 |

`desktop-build-lock.json` pins runtime, wheel, and Linux library inputs by URL,
size, and SHA-256. The Intel dependency override in `build_desktop.py` is
intentional. Lock refresh with `--prepare-lock` requires reviewing the resulting
inputs; do not silently refresh it during an unrelated fix.

```sh
.desktop-venv/bin/python -B build_release.py
.desktop-venv/bin/python -B build_desktop.py --all
# Or build one target:
.desktop-venv/bin/python -B build_desktop.py --target macos-x86_64
```

Mac assembly requires Apple compiler tools and `iconutil`; Windows launcher
assembly requires MinGW-w64 GCC and windres. The Linux launcher is a checked
prebuilt ELF with source/binary hashes; after changing its C source, rebuild
on Linux using `packaging/build-linux-launcher.sh`.

Release procedure:

1. Update `ios_timeout/__init__.py`, user-facing version references, changelog,
   and verification scope when releasing behavior changes. Commit the final
   sources, then build from that clean revision. `--allow-dirty` is only for
   local development; the updater rejects such packages.
2. Build four desktop ZIPs and one source ZIP, each with its `.zip.sha256` file:
   ten uploaded assets total. Source ZIPs and desktop ZIPs are not interchangeable.
3. Verify complete inventories, modes, architecture/deployment dependencies,
   revision, checksums, and signatures. Run packaged tests against packaged
   modules and bundled Python, not accidentally against the working checkout.
   Check signatures again after execution. macOS signing is ad-hoc; no
   Developer ID notarization or Windows Authenticode signature is claimed.
4. Exercise updater install, rollback, and reactivation in temporary state.
   Check all four platform asset selections. Preserve the original launcher
   and previous version; never replace a live app's files in place.
5. When publication is part of the task, create a draft at the exact commit,
   upload all assets, record actual validation limits, then publish. The
   baseline release is a prerelease. Do not replace its assets with a new build
   under the same version. Use a notes file to preserve multiline release text.
6. Verify published asset digests and perform a published-download/update check.
   Distinguish a staged version from the running GUI; restart activates it.

The main Updates action is Update now: `desktop_updates.install_latest` checks
and installs in one cancellable background task, then the GUI offers Restart.
No browser, token, file selection or second install choice is needed. Include
prereleases by default because current releases are prereleases. Do not chain
another GUI task from a result callback while the first task is still busy.
Already-current checks do not download or alter the active version. Checks are
user-initiated; never restart during a device operation. Keep the local ZIP and
checksum installer, rollback, links and optional token under Advanced options.
The repository is public; desktop checks need no token. Optional read tokens
remain in memory. Both installation paths validate platform, clean revision,
paths, size bounds and file inventory before activation. A local checksum proves
integrity, not publisher identity. Preserve damaged-update fallback and rollback.
The pre-2.3 rename requires opening a new Openup download once; do not promise
that older package-name expectations can install the renamed layout.

## Local continuation context and remaining work

These paths describe the original development workspace; they are not portable
build dependencies:

- Checkout: `/Users/user/crosstalk-workspaces/chat-2026-10-20/iphone-timeout-reset`.
- Ignored `build/boot-handoff-review/` contains diagnostic scripts, logs, final
  payload builds, and the final `refresh-root-bundle.py` helper. Review inputs
  before reuse. Do not run `build/armv6-research/refresh-distribution.py`: it
  copies obsolete source and can overwrite the final payload changes.
- Ignored `build/release-qa/` contains archive, packaged-runtime, and updater
  checks plus release notes. Some helper filenames retain an older version
  suffix; inspect arguments and assertions before reuse. The 2.3.1 published
  updater and Mac staging checks completed successfully.
- Session-provided remote access uses
  `ssh -F /Users/user/.crosstalk/ssh/config macbook`; announce remote commands.
  Use the remote login shell when tools require its environment. Read-only USB
  inventory is `system_profiler SPUSBHostDataType`. Normal device lists do not
  establish absence of a phone in DFU or recovery.
- The remote MacBook is Apple Silicon with Rosetta. The installed launcher is
  `~/Applications/Openup.app`; its bundled Python is under
  `Contents/Resources/app/runtime/bin/python3`. Version 2.3.1 was staged through
  the existing 2.3.0 updater, and launcher dispatch/self-test passed. Confirm
  the running window's version rather than assuming it restarted.
- The latest diagnostic left the phone running its RAM disk. That is historical
  state, not permission to skip fresh identity checks. Do not terminate open
  GUI operations or reuse a temporary firmware image without checking its
  provenance and current device state.

As of 2.3.12 the **iPhone 3G timer reset is physically verified**: the 3.1.3
ramdisk boot, USB SSH, mount, plist commit + read-back, and restore recovery all
completed on a connected iPhone1,2 / iOS 4.2.1 over a USB-C hub on an Apple
Silicon Mac. Still outstanding: **visible on-screen timer/lockout clearance after
the phone reboots, the separate passcode-recovery (4.x bruteforce) flow, physical
Intel-Mac USB, execution on Monterey hardware, native Windows/Linux execution,
and WSL USB forwarding**. Packaging and Rosetta success do not establish those.
Preserve the distinction between a verified reset commit and confirmed on-screen
clearance (which needs the phone to boot) when planning and reporting.
