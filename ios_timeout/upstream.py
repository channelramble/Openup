"""Pinned integration with tuanemss/32bit-Bruteforce-Passcode.

The upstream host backend and firmware are downloaded separately. The app adds
source-built ARMv6 payloads for iPhone 3G. Only the checked upstream revision is
adapted; unknown scripts are rejected.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

from .backend_support import check_payloads
from .armv6_payloads import extract_bundled_payloads, read_bundled_payloads
from .core import ResetError

REPOSITORY = "https://github.com/tuanemss/32bit-Bruteforce-Passcode.git"
REVISION = "4949674b318767de83eb149e57bcc543a6453d6e"
SCRIPT_SHA256 = "c6990c56f9631c91d1673246fa794e275e210211e5538a8dcf3b647a4649075b"


def checkout_backend(directory):
    directory = Path(directory).expanduser().absolute()
    if directory.is_symlink():
        raise ResetError("The backend checkout must not be a symlink.")
    if not directory.exists():
        if not shutil.which("git"):
            raise ResetError("Install Git to download the pinned backend.")
        directory.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory.parent, prefix=".download-") as temporary:
            checkout = Path(temporary) / "repo"
            for command in (
                ["git", "clone", "--filter=blob:none", "--no-checkout", REPOSITORY, str(checkout)],
                ["git", "-C", str(checkout), "checkout", "--detach", REVISION],
            ):
                subprocess.run(command, check=True, timeout=900,
                               env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            checkout.rename(directory)
    revision = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    if revision != REVISION:
        raise ResetError(f"Backend revision differs from the reviewed revision {REVISION}.")
    subprocess.run(["git", "-C", str(directory), "diff", "--quiet", "HEAD", "--"],
                   check=True, timeout=30)
    return directory


HOOK = r'''
bl_ios_send() {
    # Linux/WSL includes a sudo prefix; the backend tool path is space-free.
    ${irecovery} -f "$1" || error "Firmware upload failed; boot cancelled."
}

bl_ios_validate() {
    local resolved payloads recovery
    recovery="$($irecovery -q 2>/dev/null)"
    resolved="$("$BL_IOS_PYTHON" -m ios_timeout.backend_support identity \
        "$device_type" "$device_model" "${BL_IOS_MODEL:-}" "$recovery")" ||
        error "Device identity verification failed."
    IFS=$'\t' read -r device_type device_model <<< "$resolved"
    all_flash="Firmware/all_flash/all_flash.${device_model}ap.production"
    device_fw_dir="$PROJECT_ROOT/saved/firmware/$device_type"
    mkdir -p "$device_fw_dir" "$PROJECT_ROOT/saved/$device_type"
    device_get_name
    log "Verified target: $device_type / ${device_model}ap"
    payloads="$PROJECT_ROOT/resources"
    if [[ $device_type == "iPhone1,2" && -n ${BL_IOS_ARMV6:-} ]]; then
        payloads="$BL_IOS_ARMV6"
    fi
    "$BL_IOS_PYTHON" -m ios_timeout.backend_support payloads "$device_type" "$payloads" ||
        error "On-device executables are incompatible; no boot was attempted."
    export BL_IOS_PAYLOADS="$payloads"
}
'''


def adapt_script(original):
    if hashlib.sha256(original).hexdigest() != SCRIPT_SHA256:
        raise ResetError("Upstream script changed; refusing to apply unreviewed compatibility edits.")
    source = original.decode("utf-8")

    def replace(before, after, count=1):
        nonlocal source
        if source.count(before) != count:
            raise ResetError("Upstream compatibility patch did not match exactly.")
        source = source.replace(before, after)

    replace('PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            'PROJECT_ROOT="${BL_IOS_BACKEND:?Missing backend path}"')
    replace('    device_get_info\n', '    device_get_info\n    bl_ios_validate\n')
    replace('# ==================== INIT ====================',
            HOOK + '\n# ==================== INIT ====================')
    replace('select_ramdisk_version() {\n', '''select_ramdisk_version() {
    if [[ $device_type == "iPhone1,2" ]]; then
        log "iPhone 3G: using the 4.2.1/8C148 ramdisk and bundled ARMv6 tools."
        log "This does not install or downgrade iOS."
        device_target_build="8C148"
        return
    fi
''')
    replace('ipsw_get_url() {\n    local build_id="$1"\n', '''ipsw_get_url() {
    local build_id="$1"
    if [[ $device_type == "iPhone1,2" && $build_id == "8C148" ]]; then
        ipsw_url="https://secure-appldnld.apple.com/iPhone4/061-9853.20101122.Vfgt5/iPhone1,2_4.2.1_8C148_Restore.ipsw"
        return
    fi
''')
    replace('if [[ $device_proc == 1 && $getcomp == "Kernelcache" ]]; then\n            name=',
            'if [[ $device_type != "iPhone1,2" && $device_proc == 1 && $getcomp == "Kernelcache" ]]; then\n            name=')
    replace('iBSS.$device_model.RELEASE.dfu', 'iBSS.${device_model}ap.RELEASE.dfu')
    replace('iBEC.$device_model.RELEASE.dfu', 'iBEC.${device_model}ap.RELEASE.dfu')
    replace('$curl -sL "$url" -o "$file"', '$curl -fsSL "$url" -o "$file"')
    replace('$curl -sLO "$url"', '$curl -fsSLO "$url"')
    replace('        [[ -s tmp.json ]] && break',
            '        [[ -s tmp.json ]] && "$jq" -e . tmp.json >/dev/null 2>&1 && break\n        rm -f tmp.json')
    # Legacy firmware key indexes omit unencrypted iBSS/iBEC keys. Preserve raw
    # kernel decoding for the upstream legacy BSDIFF patch.
    replace('        "$dir/xpwntool" $getcomp.orig $getcomp.dec -iv $iv -k $key -decrypt',
            '''        if [[ $device_type != "iPhone1,2" && $device_proc == 1 && $getcomp == "Kernelcache" ]]; then
            kernel_iv="$iv"; kernel_key="$key"
            "$dir/xpwntool" "$getcomp.orig" "$getcomp.dec" -iv "$iv" -k "$key" ||
                error "Legacy kernel decoding failed."
        elif [[ -n $iv && -n $key && $iv != null && $key != null ]]; then
            "$dir/xpwntool" "$getcomp.orig" "$getcomp.dec" -iv "$iv" -k "$key" -decrypt ||
                error "Component decryption failed: $getcomp"
        else
            cp "$getcomp.orig" "$getcomp.dec" || error "Component copy failed."
        fi''')
    # The original script produces Kernelcache.patched then boots the unpatched
    # Kernelcache.dec on S5L8900. Repack the actual patch output.
    kernel_line = '        $bspatch Kernelcache.dec Kernelcache.patched $PROJECT_ROOT/resources/patch/kernelcache.release.s5l8900x.patch'
    replace(kernel_line, kernel_line + '''
        "$dir/xpwntool" Kernelcache.patched Kernelcache.dec -t Kernelcache.orig -iv "$kernel_iv" -k "$kernel_key"''')
    replace('    if [[ $device_proc == 1 ]]; then\n        # S5L8900 Kernel Patching',
            '''    if [[ $device_type == "iPhone1,2" ]]; then
        cp Kernelcache.dec Kernelcache.template || error "Could not preserve kernel template."
        "$dir/xpwntool" Kernelcache.dec Kernelcache.raw
        "$BL_IOS_PYTHON" -m ios_timeout.armv6_kernel Kernelcache.raw Kernelcache.patched ||
            error "iPhone 3G 4.2.1 kernel validation failed; boot cancelled."
        "$dir/xpwntool" Kernelcache.patched Kernelcache.dec -t Kernelcache.template
    elif [[ $device_proc == 1 ]]; then
        # S5L8900 Kernel Patching''')
    replace('    if [[ $device_proc == 1 || $device_type == "iPod2,1" ]]; then\n        $bspatch iBSS.orig iBSS ',
            '''    if [[ $device_type == "iPhone1,2" ]]; then
        "$dir/xpwntool" iBSS.dec iBSS.raw
        "$BL_IOS_PYTHON" -m ios_timeout.armv6_boot ibss "$dir/iBoot32Patcher" iBSS.raw iBSS.patched ||
            error "iPhone 3G iBSS validation failed; boot cancelled."
        "$dir/xpwntool" iBSS.patched iBSS -t iBSS.dec
    elif [[ $device_proc == 1 || $device_type == "iPod2,1" ]]; then
        $bspatch iBSS.orig iBSS ''')
    replace('    "$dir/iBoot32Patcher" iBEC.raw iBEC.patched --rsa --debug -b "rd=md0 -v amfi=0xff cs_enforcement_disable=1"',
            '''    if [[ $device_type == "iPhone1,2" ]]; then
        "$BL_IOS_PYTHON" -m ios_timeout.armv6_boot ibec "$dir/iBoot32Patcher" iBEC.raw iBEC.patched ||
            error "iPhone 3G iBEC validation failed; boot cancelled."
    else
        "$dir/iBoot32Patcher" iBEC.raw iBEC.patched --rsa --debug -b "rd=md0 -v amfi=0xff cs_enforcement_disable=1"
    fi''')
    replace('        cp $PROJECT_ROOT/resources/setup.sh setup.temp',
            '''        if [[ $device_type == "iPhone1,2" ]]; then
            "$dir/hfsplus" Ramdisk.raw add "$BL_IOS_3G_WAIT" usr/local/bin/bl-ios-wait
            "$dir/hfsplus" Ramdisk.raw chmod 755 usr/local/bin/bl-ios-wait
            cp "$BL_IOS_3G_SETUP" setup.temp || error "Managed iPhone 3G startup is missing."
        else
            cp $PROJECT_ROOT/resources/setup.sh setup.temp || error "Ramdisk startup copy failed."
        fi''')
    replace('            warn "ssh_old.tar not found! Trying standard ssh.tar (might fail)..."',
            '''            [[ $device_type == "iPhone1,2" ]] && error "Required ARMv6 SSH archive is missing."
            warn "ssh_old.tar not found! Trying standard ssh.tar (might fail)..."''')
    replace('    mv iBSS iBEC DeviceTree.dec Kernelcache.dec Ramdisk.dmg $ramdisk_path 2>/dev/null',
            '    mv iBSS iBEC DeviceTree.dec Kernelcache.dec Ramdisk.dmg $ramdisk_path || error "Could not publish all ramdisk components; boot cancelled."')
    replace('for file in /*; do', 'for file in /var/root/*.plist /*; do')
    for name in ("bruteforce", "device_infos", "restored_external"):
        source = source.replace(f"$PROJECT_ROOT/resources/{name}",
                                f"${{BL_IOS_PAYLOADS:-$PROJECT_ROOT/resources}}/{name}")
    source = source.replace('$irecovery -f ', 'bl_ios_send ')
    replace('                 warn "Exploit might have failed. Please re-enter DFU and try again if it fails."',
            '                 error "S5L8900 pwnDFU verification failed. Re-enter DFU before retrying."')
    # Prevent firmware upload after a failed exploit check.
    start = source.index('            warn "Failed to detect pwned DFU mode."')
    end = source.index('\n        fi', start)
    source = source[:start] + '            error "pwnDFU verification failed. Re-enter DFU before retrying."' + source[end:]
    # Use the system usbmuxd service on Linux; never kill unrelated sessions.
    source = re.sub(r'^.*(?:sudo killall -9 usbmuxd|sudo -b \$dir/usbmuxd|killall -STOP AMP|killall -CONT AMP).*\n',
                    '', source, flags=re.MULTILINE)
    source = source.replace('    [[ $(command -v systemctl) ]] && sudo systemctl restart usbmuxd\n', '')
    source = source.replace('$(dirname "$0")/tmp$$/', '$PROJECT_ROOT/tmp$$/')
    # The bundled patchers otherwise allow later commands to hide a failure.
    lines = []
    for line in source.splitlines():
        if (re.match(r'^\s*(?:\$bspatch |"\$dir/xpwntool" )', line)
                and not line.rstrip().endswith(("||", "\\"))):
            line += ' || error "Firmware patch/decode failed; boot cancelled."'
        if re.match(r'^\s*"\$dir/hfsplus" Ramdisk.raw (?:grow|untar|add|chmod|mv) ', line):
            line += ' || error "Required ramdisk update failed; boot cancelled."'
        if line.strip() == '"$dir/hfsplus" Ramdisk.raw rm usr/local/bin/restored_external 2>/dev/null':
            line += ' || error "Could not replace stock restore startup; boot cancelled."'
        lines.append(line)
    return ("\n".join(lines) + "\n").encode()


def run_backend(args):
    if not args.apply and not args.install_only:
        print(f"Backend: {REPOSITORY}\nPinned revision: {REVISION}")
        print("Preview only. Add --apply to prepare and boot the upstream passcode-recovery ramdisk.")
        print("iPhone 3G uses bundled ARMv6 tools and a 4.2.1 recovery ramdisk. --armv6-tools is an optional override.")
        return 0
    # The managed startup's wait helper always comes from our verified bundle,
    # even when a developer overrides the three recovery executables.
    read_bundled_payloads()
    backend = checkout_backend(args.backend_dir or args.state_dir / "backends/32bit-Bruteforce-Passcode")
    patched = adapt_script((backend / "Bruteforce.sh").read_bytes())
    if not shutil.which("bash"):
        raise ResetError("Bash is required. On Windows use WSL 2.")
    # Upstream has unquoted paths. A private, space-free alias avoids changing
    # the user's source directory or moving their data.
    with tempfile.TemporaryDirectory(prefix="bl-ios-", dir="/tmp") as temporary:
        folder = Path(temporary)
        alias = folder / "backend"
        alias.symlink_to(backend, target_is_directory=True)
        script = folder / "run.sh"
        script.write_bytes(patched)
        subprocess.run(["bash", "-n", str(script)], check=True, timeout=30)
        if args.install_only:
            print(f"Pinned backend installed and adapted script syntax verified: {backend}")
            return 0
        env = {**os.environ, "BL_IOS_BACKEND": str(alias), "BL_IOS_PYTHON": sys.executable,
               "BL_IOS_MODEL": args.model or "",
               "PYTHONPATH": str(Path(__file__).resolve().parent.parent) +
               (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
        startup = folder / "iphone3g-setup.sh"
        startup.write_bytes(resources.files("ios_timeout").joinpath("data/iphone3g-setup.sh").read_bytes())
        env["BL_IOS_3G_SETUP"] = str(startup)
        bundled_payloads = extract_bundled_payloads(folder / "bundled-armv6")
        env["BL_IOS_3G_WAIT"] = str(bundled_payloads / "restored_external")
        if args.armv6_tools:
            payloads = folder / "armv6"
            payloads.symlink_to(args.armv6_tools.expanduser().resolve(), target_is_directory=True)
        else:
            payloads = bundled_payloads
        if args.model:
            check_payloads(args.model, payloads if args.model == "iPhone1,2" else backend / "resources")
        env["BL_IOS_ARMV6"] = str(payloads)
        print("Connect exactly one phone. Follow the backend's DFU and dependency prompts.", flush=True)
        print("The backend performs passcode recovery on the phone; results are not logged by this app.", flush=True)
        result = subprocess.run(["bash", str(script)], cwd=folder, env=env, check=False)
        if result.returncode:
            raise ResetError(f"Upstream backend stopped with status {result.returncode}.")
    return 0
