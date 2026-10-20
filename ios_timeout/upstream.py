"""Pinned integration with tuanemss/32bit-Bruteforce-Passcode.

The upstream host backend and firmware are downloaded separately. The app adds
source-built ARMv6 payloads for iPhone 3G. Only the checked upstream revision is
adapted; unknown scripts are rejected.
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

from .backend_support import PHONES, check_payloads, resolve_identity
from .armv6_payloads import extract_bundled_payloads, read_bundled_payloads
from .core import ResetError

REPOSITORY = "https://github.com/tuanemss/32bit-Bruteforce-Passcode.git"
REVISION = "4949674b318767de83eb149e57bcc543a6453d6e"
SCRIPT_SHA256 = "c6990c56f9631c91d1673246fa794e275e210211e5538a8dcf3b647a4649075b"


def early_dfu_state(model, recovery):
    """Classify one USB snapshot; WTF does not identify the physical board."""
    if model not in PHONES or PHONES[model][1] != 0x8900:
        raise ResetError("Select the connected original iPhone or iPhone 3G before continuing.")
    fields = dict(re.findall(r"^([A-Z]+):[ \t]*(.*?)[ \t\r]*$", recovery, re.MULTILINE))
    mode = fields.get("MODE")
    if not mode:
        return "waiting"
    if mode not in ("WTF", "DFU", "Recovery", "iBSS", "iBEC"):
        return "waiting"
    try:
        chip = int(fields["CPID"], 16)
    except (KeyError, ValueError) as exc:
        raise ResetError("USB chip identity is missing or malformed; no firmware was sent.") from exc
    if chip != 0x8900:
        raise ResetError("The connected USB device is not the selected S5L8900 iPhone.")
    if mode == "WTF":
        # libirecovery reports a generic m68/BDID 00 for this shared bootrom.
        # Only the shared WTF image may be sent until real DFU identifies it.
        return "wtf"
    if "BDID" not in fields:
        raise ResetError("USB board identity is missing; no board-specific firmware was sent.")
    resolve_identity("", "", model, recovery)
    if mode == "DFU":
        return "patched" if fields.get("SRTG") == "iBoot-636.66.3x" else "stock"
    return "waiting"


def early_dfu_summary(recovery):
    """Only non-unique USB fields belong in transition diagnostics."""
    fields = dict(re.findall(r"^([A-Z]+):[ \t]*(.*?)[ \t\r]*$", recovery, re.MULTILINE))
    values = []
    for key in ("MODE", "CPID", "BDID", "SRTG"):
        value = fields.get(key, "")
        if value:
            value = re.sub(r"[^A-Za-z0-9._ -]", "?", value)[:80]
            values.append(f"{key}={value}")
    return ", ".join(values) or "no readable USB recovery state"


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

bl_ios_command() {
    ${irecovery} -c "$1" || error "Recovery command failed: $1. Ramdisk boot stopped."
}

bl_ios_early_read() {
    # One query supplies mode, signature and board identity from the same device.
    bl_ios_recovery="$(${irecovery} -q 2>/dev/null)" || bl_ios_recovery=""
    [[ -n $bl_ios_recovery ]] && bl_ios_last_recovery="$bl_ios_recovery"
    bl_ios_state="$("$BL_IOS_PYTHON" -m ios_timeout.upstream early-state \
        "$device_type" "$bl_ios_recovery")" ||
        error "S5L8900 USB identity verification failed; boot cancelled."
}

bl_ios_early_diagnostics() {
    local summary
    summary="$("$BL_IOS_PYTHON" -m ios_timeout.upstream early-summary \
        "${bl_ios_recovery:-}")"
    warn "Current USB state: $summary"
    if [[ -z $bl_ios_recovery && -n $bl_ios_last_recovery ]]; then
        summary="$("$BL_IOS_PYTHON" -m ios_timeout.upstream early-summary "$bl_ios_last_recovery")"
        warn "Last readable USB state: $summary"
    fi
}

bl_ios_early_wait() {
    local wanted="$1" remaining=15
    [[ $platform == linux ]] && remaining=24
    log "Waiting for S5L8900 $wanted USB state (up to $remaining polls)..."
    while (( remaining > 0 )); do
        bl_ios_early_read
        [[ $bl_ios_state == "$wanted" ]] && return 0
        # An already verified patched device also satisfies the entry helper.
        [[ $wanted == wtf && $bl_ios_state == patched ]] && return 0
        ((remaining--))
        ((remaining > 0)) && sleep 1
    done
    bl_ios_early_diagnostics
    error "S5L8900 $wanted verification timed out. Force restart the phone, then repeat the button steps to enter WTF mode. Keep only this phone connected; try a direct USB connection if it does not reappear. No board-specific firmware was uploaded."
}

bl_ios_early_prepare() {
    bl_ios_last_recovery=""
    bl_ios_early_read
    [[ $bl_ios_state == wtf || $bl_ios_state == patched ]] && return 0
    if [[ $device_mode == Normal ]]; then
        device_enter_mode Recovery
    fi
    if [[ $bl_ios_state == stock ]]; then
        warn "Stock DFU is not the S5L8900 WTF entry point. Force restart the phone before repeating the button steps."
        bl_ios_early_diagnostics
    fi
    print "* iPhone WTF mode helper"
    print "* Hold TOP and HOME for 10 seconds, then release TOP and keep holding HOME."
    print "* Release both buttons when the app detects WTF mode."
    pause
    "$BL_IOS_PYTHON" -m ios_timeout.recovery_timing "$device_type" -- ${irecovery} ||
        error "USB entry was not verified; recovery cancelled."
    # Recheck after the helper before the caller sends the shared image.
    bl_ios_early_read
    if [[ $bl_ios_state != wtf && $bl_ios_state != patched ]]; then
        bl_ios_early_diagnostics
        error "USB entry mode changed before upload. Restart the recovery session."
    fi
}

bl_ios_early_before_ibss() {
    bl_ios_early_read
    if [[ $bl_ios_state != patched ]]; then
        bl_ios_early_diagnostics
        error "Verified patched S5L8900 DFU is required before iBSS; boot cancelled."
    fi
}

bl_ios_validate() {
    local resolved payloads recovery
    recovery="$($irecovery -q 2>/dev/null)"
    if [[ $device_mode == WTF ]]; then
        resolved="$("$BL_IOS_PYTHON" -m ios_timeout.upstream wtf-target \
            "${BL_IOS_MODEL:-}" "$recovery")" ||
            error "Select the connected iPhone model; WTF reports a generic board."
    else
        resolved="$("$BL_IOS_PYTHON" -m ios_timeout.backend_support identity \
            "$device_type" "$device_model" "${BL_IOS_MODEL:-}" "$recovery")" ||
            error "Device identity verification failed."
    fi
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
        log "Using the 3.1.3/7E18 iBSS for the initial boot handoff."
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
    # Keep the 3.1.3 first-stage image in its own cache. An existing 4.2.1
    # iBSS must never satisfy this download, even though its basename matches.
    replace('''        [[ -s $ramdisk_path/$name ]] && cp $ramdisk_path/$name . || "$dir/pzb" -g "${path}$name" -o "$name" "$ipsw_url"
        [[ ! -s $name ]] && error "Failed to get $name."
        [[ ! -s $ramdisk_path/$name ]] && cp $name $ramdisk_path/
        mv $name $getcomp.orig''', '''        local component_url="$ipsw_url" component_cache="$ramdisk_path"
        if [[ $device_type == "iPhone1,2" && $getcomp == "iBSS" ]]; then
            component_url="https://secure-appldnld.apple.com/iPhone/061-7468.20100202.pbnrt/iPhone1,2_3.1.3_7E18_Restore.ipsw"
            component_cache="$PROJECT_ROOT/saved/$device_type/ramdisk_7E18"
            # This component is unencrypted; the other components use 8C148 keys.
            iv=""; key=""
        fi
        mkdir -p "$component_cache" || error "Could not create component cache."
        rm -f "$name" || error "Could not clear the previous component."
        if [[ -s "$component_cache/$name" ]]; then
            cp "$component_cache/$name" "$name" || error "Could not read cached $getcomp."
        else
            "$dir/pzb" -g "${path}$name" -o "$name" "$component_url" || error "Failed to download $getcomp."
        fi
        [[ -s "$name" ]] || error "Failed to get $name."
        if [[ ! -s "$component_cache/$name" ]]; then
            cp "$name" "$component_cache/" || error "Could not cache $getcomp."
        fi
        mv "$name" "$getcomp.orig" || error "Could not stage $getcomp."''')
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
    replace('local wtf_url="http://appldnld.apple.com/iPhone/061-7481.20100202.4orot/iPhone1,1_3.1.3_7E18_Restore.ipsw"',
            'local wtf_url="https://secure-appldnld.apple.com/iPhone/061-7481.20100202.4orot/iPhone1,1_3.1.3_7E18_Restore.ipsw"')
    # The pinned pzb strips directories from -o unless -c is supplied. Download
    # a basename in a fresh staging directory, then publish the complete cache
    # file. This also prevents a failed/empty download from reaching bspatch.
    replace('''        if [[ ! -s $PROJECT_ROOT/saved/$wtf_name ]]; then
             log "Downloading WTF DFU..."
            "$dir/pzb" -g "Firmware/dfu/$wtf_name" -o "$PROJECT_ROOT/saved/$wtf_name" "$wtf_url"
        fi''', '''        if [[ ! -s "$PROJECT_ROOT/saved/$wtf_name" ]]; then
            log "Downloading WTF DFU..."
            mkdir -p "$PROJECT_ROOT/saved" || error "Could not create WTF DFU cache."
            local wtf_download
            wtf_download="$(mktemp -d "$PROJECT_ROOT/saved/.wtf-download.XXXXXX")" ||
                error "Could not stage WTF DFU download."
            if ! (cd "$wtf_download" &&
                  "$dir/pzb" -g "Firmware/dfu/$wtf_name" -o "$wtf_name" "$wtf_url"); then
                rm -rf "$wtf_download"
                error "WTF DFU download failed; boot cancelled."
            fi
            if [[ ! -f "$wtf_download/$wtf_name" || ! -s "$wtf_download/$wtf_name" ]]; then
                rm -rf "$wtf_download"
                error "WTF DFU download produced no usable file; boot cancelled."
            fi
            if ! mv "$wtf_download/$wtf_name" "$PROJECT_ROOT/saved/$wtf_name"; then
                rm -rf "$wtf_download"
                error "Could not cache WTF DFU; boot cancelled."
            fi
            rmdir "$wtf_download" || error "Could not finish WTF DFU cache update."
        fi''')
    # S5L8900 starts in WTF (USB 1222), then re-enumerates in patched DFU
    # (USB 1227). Stock DFU is not interchangeable with the WTF entry point.
    # Mirror the pinned Legacy iOS Kit transition, checking the real board
    # after the shared image and again before board-specific iBSS upload.
    replace('    [[ $device_mode == "DFU" || $device_mode == "iBSS" || $device_mode == "iBEC" ]] && device_pwnd=',
            '    [[ $device_proc != 1 && ( $device_mode == "DFU" || $device_mode == "iBSS" || $device_mode == "iBEC" ) ]] && device_pwnd=')
    replace('    if [[ $device_mode == "iBSS" || $device_mode == "iBEC" ]]; then',
            '    if [[ $device_proc != 1 && ( $device_mode == "iBSS" || $device_mode == "iBEC" ) ]]; then')
    replace('''        [[ $device_mode != "DFU" ]] && device_enter_mode DFU
        if [[ $device_pwnd == "Pwnage 2.0" ]]; then
            log "Already in Pwnage 2.0 mode."
            return
        fi''', '''        bl_ios_last_recovery=""
        bl_ios_early_read
        if [[ $bl_ios_state == patched ]]; then
            device_mode="DFU"
            device_pwnd="Pwnage 2.0"
            log "Already in verified patched S5L8900 DFU; skipping WTF upload."
            return
        fi''')
    replace('''        # Send Exploit
        log "Sending Pwnage 2.0 exploit..."
        bl_ios_send "$wtf_patched"''', '''        # Prepare the shared file before the button helper; WTF may be brief.
        bl_ios_early_prepare
        if [[ $bl_ios_state == patched ]]; then
            device_mode="DFU"
            device_pwnd="Pwnage 2.0"
            log "Already in verified patched S5L8900 DFU; skipping WTF upload."
            return
        fi
        log "Sending Pwnage 2.0 exploit from verified WTF mode..."
        bl_ios_send "$wtf_patched"''')
    replace('''        sleep 2
\x20\x20\x20\x20\x20\x20\x20\x20
        # Verify
        device_pwnd="$($irecovery -q | grep "PWND" | cut -c 7-)"
        if [[ -z $device_pwnd ]]; then
             # Check SRTG just in case
             local srtg="$($irecovery -q | grep "SRTG" | cut -c 7-)"
             if [[ $srtg == "iBoot-636.66.3x" ]]; then
                 log "Exploit success (SRTG verified)."
                 device_pwnd="Pwnage 2.0"
             else
                 warn "Exploit might have failed. Please re-enter DFU and try again if it fails."
             fi
        else
             log "Exploit success: $device_pwnd"
        fi''', '''        bl_ios_early_wait patched
        device_mode="DFU"
        device_pwnd="Pwnage 2.0"
        log "Patched S5L8900 DFU verified: board identity and SRTG iBoot-636.66.3x."''')
    replace('''    elif [[ $device_proc == 1 || $device_type == "iPod2,1" ]]; then
        log "Sending iBSS..."; bl_ios_send $ramdisk_path/iBSS; sleep 2''',
            '''    elif [[ $device_proc == 1 || $device_type == "iPod2,1" ]]; then
        [[ $device_proc == 1 ]] && bl_ios_early_before_ibss
        log "Sending iBSS..."; bl_ios_send $ramdisk_path/iBSS; sleep 2''')
    # Keep a completed transfer separate from a verified boot. The old backend
    # printed only the requested mode and lost the actual DFU state on timeout.
    start = source.index("boot_sshrd() {\n")
    end = source.index("\n}\n", start) + 3
    section = source[start:end]
    if section.count("        device_find_mode Recovery\n") != 1 or section.count("\n    device_find_mode Recovery\n") != 1:
        raise ResetError("The reviewed ramdisk transition layout changed.")
    transition = '''if [[ $device_type == "iPhone1,2" ]]; then
        "$BL_IOS_PYTHON" -m ios_timeout.recovery_timing --after-ibss "$device_type" -- ${irecovery} ||
            error "iBSS did not reach verified Recovery mode; no ramdisk was sent."
        device_mode=Recovery
    else
        device_find_mode Recovery
    fi'''
    section = section.replace("device_find_mode Recovery", transition)
    section = section.replace('$irecovery -c ', 'bl_ios_command ')
    section = section.replace('log "Booting..."', 'log "Ramdisk launch command sent; startup is not yet verified."')
    section = section.replace('log "Bruteforce will auto-run on device screen."',
                              'log "When the ramdisk starts, recovery runs on the phone screen."')
    source = source[:start] + section + source[end:]
    source = source.replace('log "Ramdisk boot sequence complete."',
                            'log "Boot commands finished. Ramdisk startup still needs verification."')
    # Branding in messages only; preserve third-party URLs, file names and hashes.
    source = source.replace(' BRUTEFORCE 32bit-DEVICE', ' Openup · legacy iPhone recovery')
    source = source.replace('Usage: ./Bruteforce.sh [Options]', 'Usage: Openup recover [Options]')
    source = source.replace('log "Adding bruteforce tools to ramdisk..."', 'log "Adding recovery tools to ramdisk..."')
    source = source.replace('log "bruteforce auto enable"', 'log "Recovery startup enabled"')
    source = source.replace('echo "::    Bruteforce Passcode 32bit IOS Device"', 'echo "::    Openup · legacy iPhone recovery"')
    # Prevent firmware upload after a failed exploit check.
    start = source.index('            warn "Failed to detect pwned DFU mode."')
    end = source.index('\n        fi', start)
    source = source[:start] + '            error "pwnDFU verification failed. Re-enter DFU before retrying."' + source[end:]
    # The Python guardian owns exact Mac process identities and restores them
    # even if this shell dies. Keep upstream's global killall out of that lease.
    # Linux continues to use its existing system usbmuxd service.
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
    from .backend_ui import adapt_prompts
    return adapt_prompts("\n".join(lines) + "\n", backend="recover").encode()


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
        print("Recovery results may appear in session output and in files you explicitly export.", flush=True)
        from .macos_agents import recovery_agents
        with recovery_agents():
            result = subprocess.run(["bash", str(script)], cwd=folder, env=env, check=False)
        if result.returncode:
            raise ResetError(f"Upstream backend stopped with status {result.returncode}.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Internal recovery transition validation")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("early-state", "wtf-target"):
        subparser = commands.add_parser(command)
        subparser.add_argument("model")
        subparser.add_argument("recovery")
    commands.add_parser("early-summary").add_argument("recovery")
    args = parser.parse_args()
    try:
        if args.command == "early-summary":
            print(early_dfu_summary(args.recovery))
        else:
            state = early_dfu_state(args.model, args.recovery)
            if args.command == "wtf-target":
                if state != "wtf":
                    raise ResetError("USB mode changed; reconnect and retry.")
                print(f"{args.model}\t{PHONES[args.model][0]}")
            else:
                print(state)
        return 0
    except ResetError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
