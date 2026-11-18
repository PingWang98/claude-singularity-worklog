"""Proxy-only exit geolocation and validated, session-scoped locale settings."""
import base64
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
import ssl
from zoneinfo import ZoneInfo

STORAGE = Path('/mnt/example/runtime-data')
ZONE_ROOT = STORAGE / 'images/python-rootfs/usr/share/zoneinfo'
LOCALE_ROOT = STORAGE / 'region-data/current/usr/lib/locale'

# Regional defaults, not a claim that every person in a country uses one language.
LANGUAGES = {
    **dict.fromkeys(('US', 'GB', 'CA', 'AU', 'NZ', 'IE', 'SG', 'IN', 'PH', 'ZA'), 'en'),
    'JP': 'ja', 'KR': 'ko', 'CN': 'zh', 'TW': 'zh', 'HK': 'zh',
    'DE': 'de', 'AT': 'de', 'CH': 'de', 'FR': 'fr', 'BE': 'nl', 'NL': 'nl',
    'SE': 'sv', 'FI': 'fi', 'NO': 'nb', 'DK': 'da', 'ES': 'es', 'MX': 'es',
    'AR': 'es', 'CL': 'es', 'PT': 'pt', 'BR': 'pt', 'IT': 'it', 'PL': 'pl',
    'CZ': 'cs', 'RO': 'ro', 'HU': 'hu', 'GR': 'el', 'ID': 'id', 'MY': 'ms',
    'VN': 'vi', 'IL': 'he', 'TR': 'tr', 'UA': 'uk', 'RU': 'ru', 'AE': 'ar', 'SA': 'ar',
}
PUBLIC_KEYS = ('country_code', 'timezone', 'locale', 'language', 'language_tag', 'language_fallback')


def public_settings(settings):
    return {key: settings[key] for key in PUBLIC_KEYS}


def proxy_json(port, auth, host, path):
    connection = http.client.HTTPSConnection('127.0.0.1', port, timeout=8,
                                             context=ssl.create_default_context())
    authorization = base64.b64encode(auth.encode()).decode('ascii')
    connection.set_tunnel(host, 443, headers={'Proxy-Authorization': 'Basic ' + authorization})
    try:
        connection.request('GET', path, headers={'Host': host, 'User-Agent': 'claude-singularity-region-check'})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError('region_http_error')
        body = response.read(65537)
        if len(body) > 65536:
            raise RuntimeError('region_response_too_large')
        return json.loads(body)
    finally:
        connection.close()


def validate_region(data, address, zone_root=ZONE_ROOT, locale_root=LOCALE_ROOT):
    if not isinstance(data, dict) or data.get('success') is False or data.get('error') is True:
        raise RuntimeError('region_response_invalid')
    try:
        if ipaddress.ip_address(data.get('ip', '')) != ipaddress.ip_address(address):
            raise ValueError()
    except ValueError:
        raise RuntimeError('region_address_mismatch') from None
    country = str(data.get('country_code', '')).strip().upper()
    if not re.fullmatch('[A-Z]{2}', country):
        raise RuntimeError('region_country_missing')
    timezone = data.get('timezone')
    if isinstance(timezone, dict):
        timezone = timezone.get('id')
    if not isinstance(timezone, str) or not re.fullmatch(r'[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+', timezone):
        raise RuntimeError('region_timezone_invalid')
    root = Path(zone_root).resolve(strict=True)
    zonefile = (root / timezone).resolve()
    if not zonefile.is_relative_to(root) or not zonefile.is_file():
        raise RuntimeError('region_timezone_unavailable')
    try:
        with zonefile.open('rb') as stream:
            ZoneInfo.from_file(stream)
    except (ValueError, OSError):
        raise RuntimeError('region_timezone_invalid') from None
    fallback = country not in LANGUAGES
    language = LANGUAGES.get(country, 'en')
    territory = 'US' if fallback else country
    locale = language + '_' + territory + '.UTF-8'
    locale_root = Path(locale_root).resolve(strict=True)
    # UTF-8-only glibc locales such as en_IN and vi_VN omit the codeset suffix.
    if not any((locale_root / name).is_dir() for name in
               (locale.replace('.UTF-8', '.utf8'), locale.split('.')[0])):
        raise RuntimeError('region_locale_data_missing')
    return {'country_code': country, 'timezone': timezone, 'locale': locale,
            'language': language + '_' + territory + ':' + language,
            'language_tag': language + '-' + territory, 'language_fallback': fallback,
            'zonefile': str(zonefile), 'zone_sha256': hashlib.sha256(zonefile.read_bytes()).hexdigest(),
            'locale_root': str(locale_root)}


def lookup_region(port, auth, address):
    address = ipaddress.ip_address(address)
    if address.version != 4 or not address.is_global:
        raise RuntimeError('region_exit_address_invalid')
    providers = (
        ('ipwho.is', '/' + str(address) + '?fields=success,ip,country_code,timezone.id'),
        ('api.ip.sb', '/geoip/' + str(address)),
    )
    for host, path in providers:
        try:
            return validate_region(proxy_json(port, auth, host, path), address)
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            continue
    raise RuntimeError('出口地区/时区/语言查询失败；未启动 Claude，无直连或宿主设置回退')
