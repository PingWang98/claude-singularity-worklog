#!/usr/bin/env python3
"""Invoke Claude through the protected Singularity entry and existing relay."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys

PROTECTED_CLI = Path.home() / '.local/bin/claude-singularity'


def project_context(projects):
    lines = ['你在受保护的容器内运行，主项目和当前目录是 /workspace。',
             '任务中的宿主路径请按下列映射访问；这些目录直接读写原文件：',
             '- %s -> /workspace' % projects[0]]
    used = set()
    for project in projects[1:]:
        name = project.name
        suffix = 2
        while name in used:
            name = project.name + '-' + str(suffix)
            suffix += 1
        used.add(name)
        lines.append('- %s -> /projects/%s' % (project, name))
    return '\n'.join(lines)


def relay_env(profile):
    env = os.environ.copy()
    if not (env.get('ANTHROPIC_BASE_URL', '').strip() and
            (env.get('ANTHROPIC_AUTH_TOKEN', '').strip() or
             env.get('ANTHROPIC_API_KEY', '').strip())):
        if profile is None or not profile.is_file():
            raise RuntimeError('Credential profile not found.')
        # Pass the path as an argv value; never interpolate profile text into code.
        result = subprocess.run(
            ['bash', '-c', 'source "$1" >/dev/null 2>&1; status=$?; '
             '[ "$status" -eq 0 ] || exit "$status"; env -0',
             'claude-relay', str(profile)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        if result.returncode:
            raise RuntimeError('Credential profile could not be loaded.')
        env = {}
        for entry in result.stdout.split(b'\0'):
            if b'=' in entry:
                key, value = entry.split(b'=', 1)
                env[os.fsdecode(key)] = os.fsdecode(value)
    for key in ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY'):
        if key in env:
            env[key] = env[key].strip()
    if not env.get('ANTHROPIC_BASE_URL'):
        raise RuntimeError('ANTHROPIC_BASE_URL is not set; subscription fallback refused.')
    if not (env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY')):
        raise RuntimeError('Relay credential is not set; subscription fallback refused.')
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cwd', type=Path, default=Path.cwd())
    parser.add_argument('--share', action='append', type=Path, default=[],
                        help='additional project directory; repeat to share more')
    parser.add_argument('--profile', type=Path, default=os.environ.get('CLAUDE_RELAY_PROFILE'))
    parser.add_argument('--model', help='override the configured model')
    parser.add_argument('--prompt-file', type=Path)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--tools', default='Read,Glob,Grep')
    parser.add_argument('--allowed-tools', default='')
    parser.add_argument('--output-format', choices=['text', 'json', 'stream-json'], default='json')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    if args.check and args.prompt_file:
        parser.error('--check and --prompt-file are mutually exclusive')
    if not args.check and not args.prompt_file:
        parser.error('provide --prompt-file or --check')
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        parser.error('--cwd must be an existing directory')
    projects = list(dict.fromkeys([cwd] + [p.expanduser().resolve() for p in args.share]))
    if any(not project.is_dir() for project in projects):
        parser.error('--share must name existing directories')
    try:
        prompt = ('只回复：Claude中转连接正常。不要调用工具。' if args.check
                  else args.prompt_file.read_text(encoding='utf-8'))
        if not prompt.strip():
            parser.error('prompt file is empty')
        env = relay_env(args.profile.expanduser() if args.profile else None)
        if not PROTECTED_CLI.is_file():
            raise RuntimeError('Protected claude-singularity entry not found; no native fallback.')
        cmd = [str(PROTECTED_CLI)] + [item for project in projects for item in ('--project', str(project))]
        cmd += ['--', '-p']
        model = args.model or env.get('ANTHROPIC_MODEL', '').strip()
        if model:
            cmd += ['--model', model]
        cmd += ['--tools', '' if args.check else args.tools,
               '--permission-mode', 'dontAsk', '--strict-mcp-config',
               '--no-session-persistence', '--output-format', args.output_format]
        if args.output_format == 'stream-json':
            cmd.append('--verbose')
        if not args.check and args.allowed_tools:
            cmd += ['--allowedTools', args.allowed_tools]
        if not args.check:
            prompt = project_context(projects) + '\n\n下面是本次任务：\n' + prompt
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                start_new_session=True)
        try:
            proc.communicate(prompt.encode('utf-8'), timeout=min(args.timeout, 60) if args.check else args.timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            print('Claude call interrupted or timed out; no automatic retry.', file=sys.stderr)
            return 124
        return proc.returncode
    except RuntimeError as exc:
        # These are controlled messages, never raw request/header exceptions.
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print('Claude launcher failed: ' + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
