"""Launch-time namespace tests and host-owned exit monitoring."""
import json
import os
import subprocess
import sys
import time

import guard
from network_monitor import ExitMonitor, probe_exit
from regional import lookup_region

# Runs inside the application network namespace with the same seccomp policy.
STARTUP_CHECK = guard.CHECK.replace('print(json.dumps(result))', '''
for family, address in ((socket.AF_INET, ('198.51.100.1',3478)),
                        (socket.AF_INET6, ('2001:db8::1',3478))):
    with socket.socket(family,socket.SOCK_DGRAM) as s:
        try:
            s.sendto(b'guard-self-check',address)
            raise RuntimeError('UDP direct route exists')
        except OSError as error:
            if error.errno != errno.ENETUNREACH: raise
print(json.dumps(result))
''')
STARTUP_CHECK = STARTUP_CHECK.replace('print(json.dumps(result))', '''
import datetime,locale,pathlib,time
from zoneinfo import ZoneInfo
region_path=pathlib.Path('/run/claude-safe/region.json')
if region_path.exists():
    settings=json.loads(region_path.read_text())
    now=int(time.time())
    node_script="""const fs=require('fs');const r=JSON.parse(fs.readFileSync('/run/claude-safe/region.json'));
const actual=Intl.DateTimeFormat().resolvedOptions();
console.log(JSON.stringify({timezone:actual.timeZone,locale:actual.locale,
expected_timezone:Intl.DateTimeFormat('en',{timeZone:r.timezone}).resolvedOptions().timeZone,
expected_locale:Intl.getCanonicalLocales(r.language_tag)[0],
offset_minutes:-new Date(Number(process.argv[1])*1000).getTimezoneOffset()}));"""
    node=json.loads(subprocess.check_output(['/opt/node/bin/node','-e',node_script,str(now)],text=True,timeout=5))
    with open('/etc/localtime','rb') as stream: system_zone=ZoneInfo.from_file(stream)
    result['region']={'timezone':os.environ.get('TZ'),'lang':os.environ.get('LANG'),
                      'lc_all':os.environ.get('LC_ALL'),'language':os.environ.get('LANGUAGE'),
                      'locale_active':locale.setlocale(locale.LC_ALL,''),
                      'system_timezone':pathlib.Path('/etc/timezone').read_text().strip(),
                      'system_locale':dict(line.split('=',1) for line in pathlib.Path('/etc/default/locale').read_text().splitlines()),
                      'process_offset':int(datetime.datetime.fromtimestamp(now).astimezone().utcoffset().total_seconds()),
                      'system_offset':int(datetime.datetime.fromtimestamp(now,system_zone).utcoffset().total_seconds()),
                      'expected_offset':int(datetime.datetime.fromtimestamp(now,ZoneInfo(settings['timezone'])).utcoffset().total_seconds()),
                      'node':node}
print(json.dumps(result))
''')


def verify_region(report, settings):
    try:
        node = report['node']
        return (report['timezone'] == settings['timezone'] == report['system_timezone'] and
                report['lang'] == settings['locale'] == report['lc_all'] == report['locale_active'] and
                report['language'] == settings['language'] and
                report['system_locale'] == {'LANG': settings['locale'], 'LC_ALL': settings['locale'], 'LANGUAGE': settings['language']} and
                report['process_offset'] == report['system_offset'] == report['expected_offset'] == node['offset_minutes'] * 60 and
                node['timezone'] == node['expected_timezone'] and node['locale'] == node['expected_locale'])
    except (KeyError, TypeError):
        return False


class Protection:
    def __init__(self, expected=None, audit=None, probe=probe_exit, region_lookup=lookup_region):
        self.expected = expected
        self.audit = audit
        self.probe = probe
        self.monitor = None
        self.check_process = None
        self.region_lookup = region_lookup

    def prepare(self, runtime, peer, auth, builder, env, launch_cwd):
        self.monitor = ExitMonitor(lambda: self.probe(peer, auth), expected=self.expected, audit=self.audit)
        self.monitor.start()
        if not self.monitor.ready.wait(15):
            raise RuntimeError('启动前出口检查超时；没有启动 Claude')
        failure = self.monitor.failure()
        if failure:
            raise RuntimeError('启动前出口检查失败: ' + failure)
        settings = None
        if hasattr(builder, 'set_region'):
            settings = self.region_lookup(peer, auth, self.monitor.expected)
            builder.set_region(settings)
            self.monitor.record('region_resolved')
            print('claude-singularity: 出口地区 %s；时区 %s；语言 %s（自动）' %
                  (settings['country_code'], settings['timezone'], settings['locale']), file=sys.stderr)
        clean = {k:v for k,v in env.items() if not k.startswith('SINGULARITYENV_ANTHROPIC')}
        browser = getattr(builder, 'application_profile', 'agent') == 'browser'
        check_script = STARTUP_CHECK.replace('"host_unix_socket_blocked",', '') if browser else STARTUP_CHECK
        self.check_process = subprocess.Popen(builder(runtime, ['/usr/bin/python3','-I','-c',check_script],os.getcwd()),
                                              cwd=launch_cwd, env=clean, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = self.check_process.communicate(timeout=35)
            if self.check_process.returncode:
                raise RuntimeError('启动前容器网络自检失败；没有启动 Claude')
            report = json.loads(out)
            browser = getattr(builder, 'application_profile', 'agent') == 'browser'
            required = ('ipv4_blocked','ipv6_blocked','child_direct_blocked') if browser else ('ipv4_blocked','ipv6_blocked','host_unix_socket_blocked','child_direct_blocked')
            if report.get('interfaces') != ['lo'] or not all(report.get(k) is True for k in required) or report.get('proxy_https_status') != 200:
                raise RuntimeError('启动前容器网络证据不完整；没有启动 Claude')
            if browser and report.get('host_unix_socket_blocked') is not False:
                raise RuntimeError('浏览器本地通信配置核验失败')
            if settings is not None:
                if not verify_region(report.get('region', {}), settings):
                    raise RuntimeError('容器时区/语言核验失败；没有启动 Claude')
                self.monitor.record('region_verified')
            if self.monitor.failure():
                raise RuntimeError('启动检查期间出口监测失效；没有启动 Claude')
            self.monitor.record('namespace_and_proxy_verified')
        except subprocess.TimeoutExpired:
            raise RuntimeError('启动前容器网络自检超时；没有启动 Claude') from None
        finally:
            guard.stop(self.check_process)
            self.check_process.communicate(timeout=5)
            self.check_process = None

    def failure(self):
        return self.monitor.failure() if self.monitor else 'monitor_missing'

    def close(self):
        guard.stop(self.check_process)
        if self.monitor:
            self.monitor.close()
