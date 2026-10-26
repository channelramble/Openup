"""Request normal boot through the pinned kit's ordinary recovery command."""

import os
import platform
import re
import subprocess
import time

from .core import ResetError
from .physical_usb import require_single_mobile_device


def _connected():
    device = require_single_mobile_device(deadline=time.monotonic() + 5)
    if device['product_id'] not in range(0x1280, 0x1284):
        raise ResetError('The phone is not in recovery mode. For a legacy iPhone in DFU, '
                         'hold Home and Power until the Apple logo appears, then release both buttons.')


def _run(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                                stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise ResetError('The recovery command timed out. Check the phone screen and USB connection.') from exc
    if result.returncode:
        raise ResetError('Could not communicate with the recovery-mode phone. Check USB access and reconnect.')
    return result


def _identity(tool, model, selector=()):
    result = _run([str(tool), *selector, '-q'])
    fields = {}
    for key, value in re.findall(r'^([A-Z]+):[ \t]*(.*?)[ \t\r]*$', result.stdout, re.MULTILINE):
        if key in fields:
            raise ResetError('Ambiguous recovery identity. Reconnect the phone and refresh.')
        fields[key] = value
    if fields.get('MODE') != 'Recovery':
        raise ResetError('Exit recovery mode requires a phone in recovery mode, not DFU.')
    if model and fields.get('PRODUCT') != model:
        raise ResetError('The connected phone does not match the selected model. Refresh the phone list.')
    try:
        identity = tuple(int(fields[key], 16) for key in ('CPID', 'BDID', 'ECID'))
        if not 0 < identity[0] <= 0xffff or not 0 <= identity[1] <= 0xff or not 0 <= identity[2] < 2**64:
            raise ValueError()
    except (KeyError, ValueError) as exc:
        raise ResetError('The phone did not provide a valid recovery identity.') from exc
    # Original iPhone and 3G have no usable ECID; retain the strict singleton gate.
    if identity[2] == 0 and fields.get('PRODUCT') not in ('iPhone1,1', 'iPhone1,2'):
        raise ResetError('The phone did not provide a usable recovery identifier.')
    return identity, fields.get('PRODUCT')


def exit_recovery(args):
    from .cli import ensure_kit
    from .macos_agents import recovery_agents

    _connected()
    kit = ensure_kit(args.kit, args.state_dir, require_pinned=True)
    if platform.system() == 'Darwin':
        tool = kit / 'bin/macos/irecovery'
    else:
        architecture = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'x86_64', 'AMD64': 'x86_64'}.get(platform.machine())
        if not architecture:
            raise ResetError('No recovery tool is available for this computer architecture.')
        tool = kit / 'bin/linux' / architecture / 'irecovery'
    if not tool.is_file() or not os.access(tool, os.X_OK):
        raise ResetError('The recovery tool is missing or not executable. Download the recovery tools again.')
    with recovery_agents():
        _connected()
        identity = _identity(tool, args.model)
        ecid = identity[0][2]
        selector = ['-i', hex(ecid)] if ecid else []
        _connected()
        if _identity(tool, args.model, selector) != identity:
            raise ResetError('The connected phone changed. No reboot was requested.')
        # -n sets auto-boot=true, saves it, and requests reboot on one connection.
        # irecovery can return zero on command errors: require its explicit success.
        result = _run([str(tool), *selector, '-v', '-n'])
        if 'Command completed successfully' not in result.stderr.splitlines():
            raise ResetError('The phone did not confirm the exit-recovery command. Check its screen before retrying.')
    print('Normal boot requested. Check the phone screen, then choose Refresh. '
          'If it returns to recovery, its installed system may need repair. No restore or erase was performed.')
    return 0
