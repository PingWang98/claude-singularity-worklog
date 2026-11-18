"""Load existing relay configuration without logging credentials."""
import os
from pathlib import Path
import subprocess

DEFAULT_MODEL = 'your-model'  # Set the deployment default or supply ANTHROPIC_MODEL.
KEYS = ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
        'ANTHROPIC_MODEL', 'CLAUDE_CODE_MAX_OUTPUT_TOKENS')


def relay_values(profile=None):
    values = {key: os.environ.get(key, '').strip() for key in KEYS}
    if not (values['ANTHROPIC_BASE_URL'] and
            (values['ANTHROPIC_AUTH_TOKEN'] or values['ANTHROPIC_API_KEY'])):
        if profile is None:
            filename = os.environ.get('CLAUDE_RELAY_PROFILE')
            profile = Path(filename).expanduser() if filename else None
        if profile is None or not profile.is_file():
            raise RuntimeError('请在宿主环境提供凭据或显式指定配置文件')
        result = subprocess.run(
            ['bash', '-c', 'source "$1" >/dev/null 2>&1; status=$?; '
             '[ "$status" -eq 0 ] || exit "$status"; env -0',
             'claude-singularity', str(profile)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        if result.returncode:
            raise RuntimeError('中转配置加载失败；请检查宿主 profile')
        loaded = {}
        for entry in result.stdout.split(b'\0'):
            if b'=' in entry:
                key, value = entry.split(b'=', 1)
                loaded[os.fsdecode(key)] = os.fsdecode(value).strip()
        # Preserve an explicit caller model while loading missing credentials.
        model = values['ANTHROPIC_MODEL']
        values = {key: loaded.get(key, '') for key in KEYS}
        if model:
            values['ANTHROPIC_MODEL'] = model
    if not values['ANTHROPIC_BASE_URL'] or not (
            values['ANTHROPIC_AUTH_TOKEN'] or values['ANTHROPIC_API_KEY']):
        raise RuntimeError('缺少中转 API 地址或密钥；请检查宿主 profile')
    values['ANTHROPIC_MODEL'] = values['ANTHROPIC_MODEL'] or DEFAULT_MODEL
    return {key: value for key, value in values.items() if value}
