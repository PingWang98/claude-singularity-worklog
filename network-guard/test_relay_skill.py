"""Exercise the maintained skill helper with a fake protected CLI, no API."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/claude-relay/scripts/run_claude.py'
spec = importlib.util.spec_from_file_location('updated_relay', SCRIPT)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class RelaySkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.relay-skill-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.prompt = self.base / 'task.txt'
        self.prompt.write_text('Read AGENTS.md and review the project; do not modify files.')
        self.result = self.base / 'captured.json'
        self.cli = self.base / 'fake-protected-cli'
        self.cli.write_text('''#!/usr/bin/python3
import json,os,pathlib,sys,time
pathlib.Path(os.environ['FAKE_RESULT']).write_text(json.dumps({'argv':sys.argv[1:],'prompt':sys.stdin.read(),'pid':os.getpid()}))
time.sleep(float(os.environ.get('FAKE_DELAY','0')))
sys.exit(int(os.environ.get('FAKE_EXIT','0')))
''')
        self.cli.chmod(0o700)

    def invoke(self, extra=(), env_extra=None, cli=None):
        env = dict(os.environ, ANTHROPIC_BASE_URL='https://dummy.invalid',
                   ANTHROPIC_AUTH_TOKEN='DUMMY', FAKE_RESULT=str(self.result))
        env.pop('ANTHROPIC_MODEL', None)
        env.update(env_extra or {})
        argv = ['run_claude.py', '--cwd', str(self.project), '--prompt-file', str(self.prompt)] + list(extra)
        with mock.patch('sys.argv', argv), mock.patch.object(relay, 'PROTECTED_CLI', cli or self.cli), \
                mock.patch.object(relay, 'relay_env', return_value=env), \
                contextlib.redirect_stderr(io.StringIO()):
            return relay.main()

    def test_protected_launcher_model_readonly_tools_and_prompt_mapping(self):
        self.assertEqual(self.invoke(), 0)
        captured = json.loads(self.result.read_text())
        self.assertEqual(captured['argv'][:3], ['--project', str(self.project), '--'])
        self.assertNotIn('--model', captured['argv'])  # The protected entry selects its default.
        self.assertEqual(captured['argv'][captured['argv'].index('--tools') + 1], 'Read,Glob,Grep')
        self.assertIn(str(self.project) + ' -> /workspace', captured['prompt'])
        self.assertTrue(captured['prompt'].endswith(self.prompt.read_text()))

    def test_multiple_shared_roots_and_model_override(self):
        first, second = self.base / 'a/shared', self.base / 'b/shared'
        first.mkdir(parents=True);second.mkdir(parents=True)
        self.assertEqual(self.invoke(['--share', str(first), '--share', str(second), '--share', str(first),
                                      '--model', 'explicit-model'], {'ANTHROPIC_MODEL': 'env-model'}), 0)
        captured = json.loads(self.result.read_text())
        self.assertEqual(captured['argv'].count('--project'), 3)
        self.assertIn(str(first) + ' -> /projects/shared', captured['prompt'])
        self.assertIn(str(second) + ' -> /projects/shared-2', captured['prompt'])
        self.assertEqual(captured['argv'][captured['argv'].index('--model') + 1], 'explicit-model')

    def test_environment_model_is_respected(self):
        self.assertEqual(self.invoke(env_extra={'ANTHROPIC_MODEL': 'env-model'}), 0)
        args = json.loads(self.result.read_text())['argv']
        self.assertEqual(args[args.index('--model') + 1], 'env-model')

    def test_missing_protected_cli_does_not_fall_back(self):
        self.assertEqual(self.invoke(cli=self.base / 'absent'), 2)
        self.assertFalse(self.result.exists())

    def test_failure_is_returned_without_retry(self):
        self.assertEqual(self.invoke(env_extra={'FAKE_EXIT': '17'}), 17)

    def test_timeout_stops_process(self):
        self.assertEqual(self.invoke(['--timeout', '1'], {'FAKE_DELAY': '30'}), 124)
        pid = json.loads(self.result.read_text())['pid']
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
