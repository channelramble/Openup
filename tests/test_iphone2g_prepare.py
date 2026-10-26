"""Original-iPhone preparation gates, with simulated USB only."""
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ios_timeout import backend_entry, cli, legacy_kit, recovery_timing
from ios_timeout.core import ResetError


DFU = 'MODE: DFU\nCPID: 8900\nBDID: 00\nECID: 0000000000000000\nSRTG: iBoot-636.66.3x\n'
WTF = 'MODE: WTF\nCPID: 8900\nBDID: 00\nPRODUCT: iPhone1,1\nMODEL: m68ap\n'
RECOVERY = DFU.replace('MODE: DFU', 'MODE: Recovery')
IBSS = 'iBSS for m68ap, Copyright Apple Inc.\nBUILD_TAG: iBoot-636.66.33\n'
SINGLETON = {'product_id': 0x1227, 'location': 'fixture-port'}
PINNED = os.environ.get('BL_IOS_LEGACY_SCRIPT')


class OriginalPhoneIdentityTests(unittest.TestCase):
    def test_wtf_is_provisional_and_only_real_patched_m68_authorizes_ibss(self):
        self.assertEqual(legacy_kit.select_2g_identity('prepare', 'WTF', '', '', WTF), ('iPhone1,1', 'm68'))
        self.assertEqual(legacy_kit.select_2g_identity('upload', 'DFU', '', '', DFU), ('iPhone1,1', 'm68'))
        for mode, snapshot in (('WTF', WTF), ('Recovery', RECOVERY),
                               ('DFU', DFU.replace('BDID: 00', 'BDID: 04')),
                               ('DFU', DFU.replace('CPID: 8900', 'CPID: 8950')),
                               ('DFU', DFU.replace('iBoot-636.66.3x', 'iBoot-359.3')),
                               ('DFU', DFU.replace('ECID: 0000000000000000\n', '')),
                               ('DFU', DFU + 'BDID: 00\n')):
            with self.subTest(mode=mode, snapshot=snapshot), self.assertRaises(ResetError):
                legacy_kit.select_2g_identity('upload', mode, '', '', snapshot)

    def test_zero_ecid_exception_requires_exact_original_iphone(self):
        self.assertEqual(backend_entry.recovery_identity('iPhone1,1', RECOVERY, '0', 'Recovery')[3], '0')
        self.assertIsNone(backend_entry.recovery_identity('iPhone1,2', RECOVERY.replace('BDID: 00', 'BDID: 04'), '', 'Recovery'))
        with self.assertRaises(ResetError):
            backend_entry.recovery_identity('iPhone1,1', RECOVERY.replace('BDID: 00', 'BDID: 04'), '0', 'Recovery')
        values = {'ProductType': 'iPhone1,1', 'HardwareModel': 'M68AP', 'UniqueChipID': 0}
        self.assertEqual(backend_entry._normal_identity(values, 'fixture-serial', 'iPhone1,1')[3], '0')
        with self.assertRaises(backend_entry.UnreadableNormalDevice):
            backend_entry._normal_identity(values, 'fixture-serial', '')
        with self.assertRaises(ValueError):
            backend_entry._ecid(0)

    def test_live_identity_rejects_multiple_physical_phones_before_query(self):
        with patch('ios_timeout.physical_usb.require_single_mobile_device', side_effect=ResetError('multiple phones')), \
             patch('ios_timeout.recovery_timing.RecoveryProbe') as probe, self.assertRaisesRegex(ResetError, 'multiple'):
            legacy_kit.read_2g_identity('upload', 'DFU', '', '', ['fake-query'])
        probe.assert_not_called()

    def test_live_identity_checks_singleton_again_after_atomic_query(self):
        probe = Mock()
        probe.poll.return_value = DFU
        with patch('ios_timeout.physical_usb.require_single_mobile_device', side_effect=[SINGLETON, ResetError('phone appeared')]), \
             patch('ios_timeout.recovery_timing.RecoveryProbe', return_value=probe), \
             self.assertRaisesRegex(ResetError, 'appeared'):
            legacy_kit.read_2g_identity('upload', 'DFU', '', '', ['fake-query'])
        probe.close.assert_called_once()

    def test_zero_ecid_recovery_wait_still_requires_singleton(self):
        probe = Mock()
        probe.poll.return_value = RECOVERY
        with patch('ios_timeout.physical_usb.require_single_mobile_device', side_effect=ResetError('inventory unavailable')), \
             patch('ios_timeout.recovery_timing.RecoveryProbe', return_value=probe), \
             self.assertRaisesRegex(ResetError, 'inventory unavailable'):
            backend_entry.wait_mode('iPhone1,1', '0', ['fake-query'], timeout=1)
        probe.close.assert_called_once()

    def test_exact_m68_stage_build_and_board_are_required(self):
        self.assertIn('m68ap', recovery_timing.verify_bootloader_console('iPhone1,1', IBSS))
        for console in (IBSS.replace('iBSS', 'iBoot'), IBSS.replace('m68ap', 'n82ap'),
                        IBSS.replace('636.66.33', '931.71.16'), IBSS + IBSS.replace('iBSS', 'iBoot')):
            with self.subTest(console=console), self.assertRaises(ResetError):
                recovery_timing.verify_bootloader_console('iPhone1,1', console)
        with self.assertRaises(ResetError):
            recovery_timing.verify_bootloader_console('iPhone1,1', IBSS.replace('iBSS', 'iBEC'), 'ibec')
        with self.assertRaises(ResetError):
            recovery_timing._boot_query_state('iPhone1,1', RECOVERY.replace('BDID: 00', 'BDID: 04'))

    def test_console_success_is_rechecked_against_physical_inventory(self):
        probe = Mock()
        probe.poll.return_value = RECOVERY
        probe.bootloader_console.return_value = IBSS
        with patch('ios_timeout.physical_usb.require_single_mobile_device', side_effect=[SINGLETON, ResetError('another phone')]), \
             self.assertRaisesRegex(ResetError, 'another phone'):
            recovery_timing.wait_bootloader('iPhone1,1', probe, timeout=1)
        probe.close.assert_called_once()

    def test_2g_write_commands_stop_before_preparation_or_usb(self):
        for arguments in (['reset', '--model', 'iPhone1,1'], ['prepare', '--model', 'iPhone1,1', '--apply']):
            with self.subTest(arguments=arguments), patch.object(cli, 'launch_backend') as launch, \
                 patch.object(cli.usbmux, 'wait_for_device') as wait, self.assertRaisesRegex(ResetError, 'timer reset'):
                cli.run(cli.parser().parse_args(arguments))
            launch.assert_not_called()
            wait.assert_not_called()

    def test_unknown_2g_backend_is_not_executed(self):
        with tempfile.TemporaryDirectory() as folder:
            kit = Path(folder)
            (kit / 'resources').mkdir()
            (kit / 'restore.sh').write_text('#!/bin/bash\n')
            args = cli.parser().parse_args(['prepare', '--model', 'iPhone1,1', '--kit', folder])
            with patch.object(cli.platform, 'system', return_value='Darwin'), \
                 patch.object(cli.subprocess, 'run') as run, self.assertRaises(ResetError):
                cli.launch_backend(args)
            run.assert_not_called()

    def test_read_only_prepare_waits_for_new_ram_session_and_never_builds_timer_plan(self):
        device = Mock()
        device.inspect.return_value = SimpleNamespace(model='iPhone1,1', version='3.1.3')
        session = 'a' * 40
        args = cli.parser().parse_args(['prepare', '--model', 'iPhone1,1', '--udid', 'original-normal-serial'])
        with patch.object(cli, 'host_lock', return_value=nullcontext()), \
             patch('ios_timeout.macos_agents.recovery_agents', return_value=nullcontext()), \
             patch.object(cli, 'launch_backend', return_value=session), \
             patch.object(cli.usbmux, 'wait_for_device', return_value={}) as wait, \
             patch.object(cli, 'USBDevice', return_value=nullcontext(device)), \
             patch.object(cli, 'make_plan') as plan, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.run(args), 0)
        self.assertEqual(wait.call_args.args[0], session)
        self.assertEqual(wait.call_args.kwargs['timeout'], 45)
        self.assertTrue(device.inspect.call_args.kwargs['read_only'])
        plan.assert_not_called()
        self.assertIn('Read-only iPhone 2G inspection complete', output.getvalue())


@unittest.skipUnless(PINNED and shutil.which('bash') and os.name != 'nt', 'Pinned POSIX Legacy Kit fixture required')
class OriginalPhonePinnedShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = Path(PINNED).read_bytes()
        cls.source = legacy_kit.adapt_script(cls.original, 'iPhone1,1').decode()

    def test_pinned_adaptation_and_checked_helper_injection(self):
        self.assertEqual(subprocess.run(['bash', '-n'], input=self.source, text=True, capture_output=True).returncode, 0)
        self.assertIn('bl_ios_entry detect || bl_ios_legacy_identity observe', self.source)
        self.assertIn('bl_ios_2g_bootloader', self.source)
        for command in ('add "$BL_IOS_2G_HELPER" usr/local/bin/openup-iphone2g',
                        'chmod 755 usr/local/bin/openup-iphone2g', 'chown 0:0 usr/local/bin/openup-iphone2g'):
            line = next(line for line in self.source.splitlines() if command in line)
            self.assertIn('|| error', line)
        with self.assertRaises(ResetError):
            legacy_kit.adapt_script(self.original + b'\n', 'iPhone1,1')

    def run_uploads(self, *, query=DFU, console=IBSS, failure='', shared=False, after=DFU):
        start = self.source.index('device_ramdisk() {\n')
        start = self.source.index('    bl_ios_legacy_identity upload\n', start)
        end = self.source.index('    log "Booting, please wait..."', start)
        with tempfile.TemporaryDirectory(prefix='2g shell ') as folder:
            root = Path(folder)
            trace, state = root / 'trace', root / 'state'
            work = root / 'work'
            work.mkdir()
            program = root / 'tool.py'
            program.write_text('''import json,os,sys
from pathlib import Path
name=sys.argv[1]; args=sys.argv[2:]
if name=='python':
 from ios_timeout import physical_usb
 physical_usb.require_single_mobile_device=lambda **kwargs: {'product_id':0x1227,'location':'fixture-port'}
 module=args[1]; sys.argv=[module]+args[2:]
 if module=='ios_timeout.legacy_kit':
  from ios_timeout.legacy_kit import main
  raise SystemExit(main())
 if module=='ios_timeout.recovery_timing':
  from ios_timeout.recovery_timing import RecoveryProbe,wait_bootloader
  wait_bootloader('iPhone1,1',RecoveryProbe(args[5:]),timeout=2)
  raise SystemExit(0)
 raise SystemExit(99)
with open(os.environ['TRACE'],'a') as f:f.write(json.dumps(args)+'\\n')
state=Path(os.environ['STATE'])
if args==['-q']:
 if not state.exists():print(os.environ['QUERY'])
 elif state.read_text()=='patched':print(os.environ['AFTER_QUERY'])
 else:print(os.environ['QUERY'].replace('MODE: DFU','MODE: Recovery'))
elif args==['-s']:
 sys.stdin.read(); print(os.environ['CONSOLE'])
else:
 if ' '.join(args)==os.environ['FAILURE']:sys.exit(9)
 if args==['-f','iBSS']:state.write_text('ibss')
 elif args[:1]==['-f'] and 'WTF' in args[1]:state.write_text('patched')
''')
            for name in ('irecovery', 'python'):
                path = root / name
                path.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -B ' + shlex.quote(str(program)) + ' ' + name + ' "$@"\n')
                path.chmod(0o700)
            body = '\nrun_uploads() {\n' + self.source[start:end] + '\n}\n'
            if shared:
                body = re.search(r'^device_s5l8900xall\(\) \{\n.*?^\}', self.source, re.M | re.S)[0]
                body += r'''
stub_sha() { echo 'cb96954185a91712c47f20adb519db45a318c30f file'; }
stub_patch() { printf patched > "$2"; }
download_with_pzb() { echo UNEXPECTED_DOWNLOAD; exit 99; }
run_uploads() { device_mode=WTF; device_s5l8900xall; }
sha1sum=stub_sha
bspatch=stub_patch
'''
            shell = legacy_kit.HOOK_2G + '\n' + body + r'''
log() { :; }
print() { :; }
error() { printf 'ERROR:%s\n' "$*" >&2; exit 73; }
sleep() { :; }
device_mode=DFU
device_type=iPhone1,1
device_model=m68
device_proc=1
build_id=7E18
platform=macos
run_uploads
echo BOOT_UPLOADS_COMPLETE
'''
            env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]), 'TRACE': str(trace),
                   'STATE': str(state), 'QUERY': query, 'CONSOLE': console, 'FAILURE': failure,
                   'AFTER_QUERY': after,
                   'irecovery': str(root / 'irecovery'), 'BL_IOS_PYTHON': str(root / 'python')}
            result = subprocess.run(['bash'], input=shell, text=True, capture_output=True, env=env, cwd=work, timeout=12)
            calls = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
            return result, [args for args in calls if args[:1] in (['-f'], ['-c'])]

    def test_actual_m68_upload_branch_requires_exact_ibss_before_ramdisk(self):
        result, uploads = self.run_uploads()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([args[1] for args in uploads if args[0] == '-f'], ['iBSS', 'Ramdisk.dmg', 'DeviceTree.dec', 'Kernelcache.dec'])
        self.assertNotIn('iBEC', str(uploads))
        self.assertIn('verified: iBSS for m68ap / iBoot-636.66.33', result.stdout)
        for console in (IBSS.replace('iBSS', 'iBoot'), IBSS.replace('m68ap', 'n82ap')):
            with self.subTest(console=console):
                result, uploads = self.run_uploads(console=console)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(uploads, [['-f', 'iBSS']])
                self.assertNotIn('BOOT_UPLOADS_COMPLETE', result.stdout)

    def test_actual_upload_branch_stops_on_wrong_phone_or_failed_transfer(self):
        result, uploads = self.run_uploads(query=DFU.replace('BDID: 00', 'BDID: 04'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(uploads, [])
        result, uploads = self.run_uploads(failure='-f iBSS')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(uploads, [['-f', 'iBSS']])
        self.assertNotIn('BOOT_UPLOADS_COMPLETE', result.stdout)

    def test_actual_shared_wtf_transition_checks_real_m68_board(self):
        for after, success in ((DFU, True), (DFU.replace('BDID: 00', 'BDID: 04'), False)):
            with self.subTest(after=after):
                result, uploads = self.run_uploads(query=WTF, shared=True, after=after)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                self.assertEqual(len(uploads), 1)
                self.assertTrue(uploads[0][1].endswith('WTF.s5l8900xall.RELEASE.dfu.patched'))
                self.assertEqual('BOOT_UPLOADS_COMPLETE' in result.stdout, success)

    def test_cli_dispatch_injects_temporary_verified_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'resources').mkdir()
            (root / 'restore.sh').write_bytes(self.original)
            args = cli.parser().parse_args(['prepare', '--model', 'iPhone1,1', '--kit', str(root)])
            launched = []
            def run(command, **kwargs):
                if command[0] == 'bash' and command[1] != '-n':
                    helper = Path(kwargs['env']['BL_IOS_2G_HELPER'])
                    self.assertTrue(helper.is_file())
                    self.assertTrue(Path(kwargs['env']['BL_IOS_2G_STARTUP']).is_file())
                    session = Path(kwargs['env']['BL_IOS_2G_SESSION'])
                    self.assertRegex(session.read_text(), r'^[0-9a-f]{40}$')
                    self.assertEqual(session.stat().st_mode & 0o777, 0o600)
                    self.assertIn('--device=iPhone1,1', command)
                    self.assertIn('--build-id=7E18', command)
                    launched.append(helper)
                return subprocess.CompletedProcess(command, 0)
            with patch.object(cli.platform, 'system', return_value='Darwin'), \
                 patch('ios_timeout.bundled_python2.environment', side_effect=lambda: os.environ.copy()), \
                 patch.object(cli.subprocess, 'run', side_effect=run), redirect_stdout(io.StringIO()):
                serial = cli.launch_backend(args)
            self.assertRegex(serial, r'^[0-9a-f]{40}$')
            self.assertEqual(len(launched), 1)
            self.assertFalse(launched[0].exists())
            self.assertEqual((root / 'restore.sh').read_bytes(), self.original)
            self.assertEqual(list(root.glob('.bl-ios-prepare-*')), [])
