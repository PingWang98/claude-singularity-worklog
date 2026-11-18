"""Proxy-only region selection and actual container locale/timezone checks."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import guard
import regional
import singularity_guard as sg
import test_singularity
from launch_protection import Protection, verify_region


def japan():
    return regional.validate_region({'ip': '8.8.8.8', 'country_code': 'JP',
                                     'timezone': {'id': 'Asia/Tokyo'}}, '8.8.8.8')


class RegionalTests(unittest.TestCase):
    def test_japanese_settings_and_locale_data(self):
        settings = japan()
        self.assertEqual(settings['timezone'], 'Asia/Tokyo')
        self.assertEqual(settings['locale'], 'ja_JP.UTF-8')
        self.assertEqual(settings['language_tag'], 'ja-JP')
        self.assertFalse(settings['language_fallback'])

    def test_all_mapped_country_defaults_have_real_locale_data(self):
        for country in regional.LANGUAGES:
            with self.subTest(country=country):
                regional.validate_region({'ip': '8.8.8.8', 'country_code': country,
                                          'timezone': 'Etc/UTC'}, '8.8.8.8')

    def test_unknown_country_language_uses_documented_english_default(self):
        settings = regional.validate_region({'ip': '8.8.8.8', 'country_code': 'AQ',
                                              'timezone': 'Antarctica/McMurdo'}, '8.8.8.8')
        self.assertEqual(settings['locale'], 'en_US.UTF-8')
        self.assertTrue(settings['language_fallback'])

    def test_invalid_region_data_refused(self):
        for patch in ({'ip': '1.1.1.1'}, {'country_code': ''}, {'timezone': '../etc/passwd'},
                      {'timezone': 'Local'}, {'timezone': 'Missing/Nowhere'}, {'success': False}):
            data = {'ip': '8.8.8.8', 'country_code': 'JP', 'timezone': 'Asia/Tokyo'}
            data.update(patch)
            with self.subTest(patch=patch), self.assertRaises(RuntimeError):
                regional.validate_region(data, '8.8.8.8')

    def test_proxy_json_ignores_environment_and_connects_only_to_private_proxy(self):
        with mock.patch.dict(os.environ, {'NO_PROXY': '*', 'HTTPS_PROXY': 'http://bad.invalid:1'}), \
                mock.patch('regional.http.client.HTTPSConnection') as factory:
            conn = factory.return_value
            conn.getresponse.return_value.status = 200
            conn.getresponse.return_value.read.return_value = b'{"success":true}'
            self.assertEqual(regional.proxy_json(1234, 'dummy:auth', 'ipwho.is', '/8.8.8.8'), {'success': True})
            self.assertEqual(factory.call_args.args, ('127.0.0.1', 1234))
            self.assertEqual(conn.set_tunnel.call_args.args, ('ipwho.is', 443))

    def test_provider_fallback_is_bounded_and_uses_same_proxy(self):
        good = {'ip': '8.8.8.8', 'country_code': 'JP', 'timezone': 'Asia/Tokyo'}
        with mock.patch('regional.proxy_json', side_effect=[TimeoutError(), good]) as query:
            self.assertEqual(regional.lookup_region(1234, 'dummy', '8.8.8.8')['locale'], 'ja_JP.UTF-8')
        self.assertEqual(query.call_count, 2)
        self.assertTrue(all(call.args[:2] == (1234, 'dummy') for call in query.call_args_list))

    def test_missing_region_prevents_application_start(self):
        with tempfile.TemporaryDirectory(prefix='.region-fail-', dir=str(guard.ROOT.parent)) as directory:
            marker = Path(directory) / 'application-started'
            def build(*args):
                return ['/usr/bin/touch', str(marker)]
            build.set_region = lambda settings: None
            def fail(*args):
                raise RuntimeError('region lookup failed')
            protection = Protection(probe=lambda *args: '8.8.8.8', region_lookup=fail)
            node = {'name': 'CLAUDE_ONLY_PROXY', 'type': 'socks5', 'server': '192.0.2.1', 'port': 1080}
            with self.assertRaisesRegex(RuntimeError, 'region lookup failed'):
                guard.run_session(node, [], str(sg.MIHOMO), builder=build,
                                  runtime_parent=directory, protection=protection)
            self.assertFalse(marker.exists())

    def test_offset_or_language_mismatch_is_not_accepted(self):
        settings = japan()
        report = {'timezone': 'Asia/Tokyo', 'system_timezone': 'Asia/Tokyo',
                  'lang': 'ja_JP.UTF-8', 'lc_all': 'ja_JP.UTF-8', 'locale_active': 'ja_JP.UTF-8',
                  'language': 'ja_JP:ja', 'process_offset': 32400, 'system_offset': 32400,
                  'expected_offset': 32400, 'node': {'offset_minutes': 540, 'timezone': 'Asia/Tokyo',
                                                  'expected_timezone': 'Asia/Tokyo', 'locale': 'ja-JP', 'expected_locale': 'ja-JP'}}
        report['system_locale'] = {'LANG': 'ja_JP.UTF-8', 'LC_ALL': 'ja_JP.UTF-8', 'LANGUAGE': 'ja_JP:ja'}
        self.assertTrue(verify_region(report, settings))
        report['system_offset'] = 0
        self.assertFalse(verify_region(report, settings))
        report['system_offset'] = 32400
        report['node']['locale'] = 'en-US'
        self.assertFalse(verify_region(report, settings))


class RegionalContainerTests(test_singularity.SingularityTests):
    def setUp(self):
        super().setUp()
        self.build.set_region(japan())

    def test_real_python_node_and_system_timezone_and_language(self):
        script = '''
import datetime,json,locale,os,pathlib,subprocess
assert os.environ['TZ']=='Asia/Tokyo'
assert os.environ['LANG']==os.environ['LC_ALL']=='ja_JP.UTF-8'
assert os.environ['LANGUAGE']=='ja_JP:ja'
assert locale.setlocale(locale.LC_ALL,'')=='ja_JP.UTF-8'
assert pathlib.Path('/etc/timezone').read_text().strip()=='Asia/Tokyo'
assert datetime.datetime.now().astimezone().utcoffset().total_seconds()==32400
node=json.loads(subprocess.check_output(['/opt/node/bin/node','-e','console.log(JSON.stringify(Intl.DateTimeFormat().resolvedOptions()))'],text=True))
assert node['timeZone']=='Asia/Tokyo' and node['locale']=='ja-JP'
try:
 fd=os.open('/etc/localtime',os.O_WRONLY);os.close(fd);raise AssertionError('timezone writable')
except OSError:pass
print('REGION_OK')
'''
        with guard.bridge(self.runtime, self.proxy.server_address[1], 'test:secret'):
            result = self.run_isolated(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'REGION_OK')

    def test_other_region_uses_its_timezone_and_language(self):
        self.build.set_region(regional.validate_region({'ip': '8.8.8.8', 'country_code': 'US',
                                                       'timezone': 'America/New_York'}, '8.8.8.8'))
        script = '''
import datetime,json,locale,os,pathlib,subprocess,time
from zoneinfo import ZoneInfo
assert os.environ['TZ']=='America/New_York'
assert os.environ['LANG']==os.environ['LC_ALL']=='en_US.UTF-8'
assert locale.setlocale(locale.LC_ALL,'')=='en_US.UTF-8'
assert pathlib.Path('/etc/timezone').read_text().strip()=='America/New_York'
now=time.time()
assert datetime.datetime.fromtimestamp(now).astimezone().utcoffset()==datetime.datetime.fromtimestamp(now,ZoneInfo('America/New_York')).utcoffset()
node=json.loads(subprocess.check_output(['/opt/node/bin/node','-e','console.log(JSON.stringify(Intl.DateTimeFormat().resolvedOptions()))'],text=True))
assert node['timeZone']=='America/New_York' and node['locale']=='en-US'
print('OTHER_REGION_OK')
'''
        with guard.bridge(self.runtime, self.proxy.server_address[1], 'test:secret'):
            result = self.run_isolated(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'OTHER_REGION_OK')

    def test_tampered_region_policy_refuses_application(self):
        import subprocess
        with guard.bridge(self.runtime, self.proxy.server_address[1], 'test:secret'):
            args = self.build(self.runtime, ['/bin/echo', 'MUST_NOT_RUN'], str(self.project))
            path = self.runtime / 'launch-1/region.json'
            data = json.loads(path.read_text());data['locale'] = 'en_US.UTF-8'
            path.write_text(json.dumps(data))
            result = subprocess.run(args, env=sg.environment(), cwd=sg.STORAGE / 'work/launch',
                                    capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('MUST_NOT_RUN', result.stdout)
        self.assertIn('region policy mismatch', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
