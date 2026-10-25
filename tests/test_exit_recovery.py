import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ios_timeout import cli, exit_recovery as recovery
from ios_timeout.core import ResetError
from ios_timeout.desktop_model import Settings, operation_arguments
from ios_timeout.desktop_results import summarize

QUERY = 'CPID: 0x8950\nBDID: 0x00\nECID: 0x1234\nMODE: Recovery\nPRODUCT: iPhone5,1\n'


class ExitRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tool = self.root / 'bin/macos/irecovery'
        tool.parent.mkdir(parents=True)
        tool.write_text('fixture')
        tool.chmod(0o755)
        self.args = SimpleNamespace(kit=None, state_dir=self.root, model='iPhone5,1')
        for target, kwargs in (
            ('ios_timeout.cli.ensure_kit', {'return_value': self.root}),
            ('ios_timeout.macos_agents.recovery_agents', {'side_effect': nullcontext}),
            ('ios_timeout.exit_recovery.platform.system', {'return_value': 'Darwin'}),
            ('ios_timeout.exit_recovery.require_single_mobile_device', {'return_value': {'product_id': 0x1281}}),
        ):
            mock = patch(target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)

    def execute(self, outputs):
        with patch.object(recovery.subprocess, 'run', side_effect=outputs) as run:
            recovery.exit_recovery(self.args)
        return run

    def result(self, stdout=QUERY, stderr='', returncode=0):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    def test_exact_phone_selected_and_success_reported(self):
        run = self.execute([self.result(), self.result(), self.result('', 'Command completed successfully\n')])
        self.assertEqual(run.call_args.args[0][1:], ['-i', '0x1234', '-v', '-n'])
        self.assertEqual(run.call_count, 3)

    def test_dfu_normal_and_multiple_devices_never_send_command(self):
        for pid in (0x1227, 0x1290):
            with patch.object(recovery, 'require_single_mobile_device', return_value={'product_id': pid}), patch.object(recovery, '_run') as run:
                with self.assertRaises(ResetError):
                    recovery.exit_recovery(self.args)
                run.assert_not_called()
        with patch.object(recovery, 'require_single_mobile_device', side_effect=ResetError('Connect exactly one')), patch.object(recovery, '_run') as run:
            with self.assertRaises(ResetError):
                recovery.exit_recovery(self.args)
            run.assert_not_called()

    def test_wrong_model_duplicate_identity_and_changed_phone_stop(self):
        for query in (QUERY.replace('iPhone5,1', 'iPhone5,2'), QUERY + 'ECID: 0x1234\n', QUERY.replace('MODE: Recovery', 'MODE: DFU')):
            with patch.object(recovery.subprocess, 'run', return_value=self.result(query)) as run:
                with self.assertRaises(ResetError):
                    recovery.exit_recovery(self.args)
                self.assertEqual(run.call_count, 1)
        with patch.object(recovery.subprocess, 'run', side_effect=[self.result(), self.result(QUERY.replace('1234', '5678'))]) as run:
            with self.assertRaises(ResetError):
                recovery.exit_recovery(self.args)
            self.assertEqual(run.call_count, 2)

    def test_zero_exit_without_success_is_not_success(self):
        with self.assertRaisesRegex(ResetError, 'did not confirm'):
            self.execute([self.result(), self.result(), self.result('', 'Unable to connect to device\n')])

    def test_timeout_is_actionable(self):
        with patch.object(recovery.subprocess, 'run', side_effect=recovery.subprocess.TimeoutExpired('irecovery', 20)):
            with self.assertRaisesRegex(ResetError, 'timed out'):
                recovery.exit_recovery(self.args)

    def test_desktop_cli_and_result(self):
        argv = operation_arguments('exit-recovery', Settings(model='iPhone5,1', udid='not-a-recovery-id'))
        args = cli.parser().parse_args(argv)
        self.assertEqual(args.model, 'iPhone5,1')
        self.assertNotIn('--udid', argv)
        self.assertIn('requested', summarize('exit-recovery', '', 0)['message'])
        self.assertIn('stopped', summarize('exit-recovery', '', 1)['message'])
