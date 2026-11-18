"""CLI defaults and credential loading, using dummy projects and credentials."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import guard
import singularity_guard as sg
from relay_credentials import DEFAULT_MODEL, relay_values


class LaunchDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.defaults-', dir=str(guard.ROOT.parent))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        (self.project / 'file').write_text('original')
        self.storage = self.base / 'storage'
        self.storage.mkdir()
        # CLI tests stop before execution, so use local stand-ins for both paths.
        image = self.storage / 'image'
        entry = image / 'opt/claude-safe/entry.py'
        entry.parent.mkdir(parents=True)
        entry.touch()
        for name, value in [('IMAGE', image), ('SINGULARITY', entry)]:
            patcher = mock.patch.object(sg, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def invoke(self, arguments):
        with mock.patch.object(sg, 'STORAGE', self.storage), \
                mock.patch('sys.argv', ['claude-singularity'] + arguments), \
                mock.patch('os.getcwd', return_value=str(self.project)), \
                mock.patch.object(sg, 'relay_values', return_value={
                    'ANTHROPIC_BASE_URL': 'https://dummy.invalid',
                    'ANTHROPIC_AUTH_TOKEN': 'DUMMY', 'ANTHROPIC_MODEL': DEFAULT_MODEL}) as credentials, \
                mock.patch.object(guard, 'current_node', return_value=('DUMMY', {})), \
                mock.patch.object(guard, 'run_session', return_value=0) as run, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sg.main(), 0)
        return run.call_args, credentials

    def test_no_arguments_directly_mounts_current_directory_with_default_model(self):
        call, credentials = self.invoke([])
        session = call.kwargs['builder'].copy
        self.assertEqual(session.workspace, self.project)
        self.assertEqual(session.metadata['mode'], 'direct')
        self.assertFalse((session.folder / 'workspace').exists())
        self.assertEqual(call.args[1], ['/opt/claude/claude'])
        self.assertEqual(call.kwargs['environment']['SINGULARITYENV_ANTHROPIC_MODEL'], DEFAULT_MODEL)
        credentials.assert_called_once()

    def test_copy_mode_and_existing_changes_apply_actions_still_work(self):
        call, _ = self.invoke(['--copy'])
        session = call.kwargs['builder'].copy
        self.assertNotEqual(session.workspace, self.project)
        (session.workspace / 'file').write_text('changed')
        self.assertEqual((self.project / 'file').read_text(), 'original')
        actions, credentials = self.invoke(['--changes', session.id])
        self.assertIsNone(actions)
        credentials.assert_not_called()
        self.invoke(['--apply', session.id])
        self.assertEqual((self.project / 'file').read_text(), 'changed')

    def test_multiple_projects_need_no_direct_flag_and_are_granted_to_claude(self):
        second = self.base / 'shared'
        second.mkdir()
        call, _ = self.invoke(['--project', str(self.project), '--project', str(second),
                               '--', '--model', 'caller-model'])
        self.assertEqual(call.kwargs['builder'].copy.projects, [self.project, second])
        self.assertEqual(call.args[1], ['/opt/claude/claude', '--add-dir', '/projects/shared',
                                      '--model', 'caller-model'])

    def test_network_check_does_not_load_api_credentials(self):
        call, credentials = self.invoke(['--check'])
        credentials.assert_not_called()
        self.assertEqual(call.kwargs['builder'].copy.metadata['mode'], 'direct')
        self.assertEqual(call.args[1], ['/bin/echo', 'Automatic network checks passed'])

    def test_browser_uses_fixed_driver_and_never_loads_or_passes_relay_credentials(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'DUMMY'}):
            call, credentials = self.invoke(['--browser', 'https://example.com/'])
        credentials.assert_not_called()
        self.assertEqual(call.kwargs['builder'].application_profile, 'browser')
        self.assertEqual(call.args[1], ['/opt/node/bin/node', '/opt/claude-safe/browser.js', 'https://example.com/'])
        self.assertFalse(any(key.startswith('SINGULARITYENV_ANTHROPIC') for key in call.kwargs['environment']))

    def test_browser_rejects_local_files_embedded_passwords_and_extra_commands(self):
        for arguments in (['--browser', 'file:///etc/passwd'],
                          ['--browser', 'https://user:pass@example.com/'],
                          ['--browser', 'https://example.com/', '--', '--no-sandbox']):
            with self.subTest(arguments=arguments), self.assertRaises(RuntimeError):
                self.invoke(arguments)


class CredentialLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.profile-', dir=str(guard.ROOT.parent))
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / 'profile with spaces'

    def test_missing_environment_loads_profile_and_default_model(self):
        self.profile.write_text("export ANTHROPIC_BASE_URL=' https://dummy.invalid '\n"
                                "export ANTHROPIC_AUTH_TOKEN=' DUMMY '\n")
        with mock.patch.dict(os.environ, {}, clear=True):
            values = relay_values(self.profile)
        self.assertEqual(values, {'ANTHROPIC_BASE_URL': 'https://dummy.invalid',
                                 'ANTHROPIC_AUTH_TOKEN': 'DUMMY', 'ANTHROPIC_MODEL': DEFAULT_MODEL})

    def test_caller_model_survives_loading_missing_credentials(self):
        self.profile.write_text("export ANTHROPIC_BASE_URL=https://dummy.invalid\n"
                                "export ANTHROPIC_API_KEY=DUMMY\n"
                                "export ANTHROPIC_MODEL=profile-model\n")
        with mock.patch.dict(os.environ, {'ANTHROPIC_MODEL': 'caller-model'}, clear=True):
            self.assertEqual(relay_values(self.profile)['ANTHROPIC_MODEL'], 'caller-model')

    def test_complete_environment_does_not_source_profile(self):
        env = {'ANTHROPIC_BASE_URL': 'https://dummy.invalid', 'ANTHROPIC_API_KEY': 'DUMMY'}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch('relay_credentials.subprocess.run') as run:
            self.assertEqual(relay_values(self.profile)['ANTHROPIC_MODEL'], DEFAULT_MODEL)
        run.assert_not_called()

    def test_invalid_configuration_has_controlled_error(self):
        self.profile.write_text('return 1\n')
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, '配置加载失败'):
                relay_values(self.profile)


if __name__ == '__main__':
    unittest.main(verbosity=2)
