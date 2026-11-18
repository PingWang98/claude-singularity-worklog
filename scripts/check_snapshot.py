#!/usr/bin/env python3
"""Offline archive integrity, syntax, links, and obvious secret-format checks."""
import ast
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent


def main():
    manifest=json.loads((ROOT/'records/runtime-manifest.json').read_text())
    for name,want in manifest['files'].items():
        assert hashlib.sha256((ROOT/'network-guard'/name).read_bytes()).hexdigest()==want,name
    files=[p for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.relative_to(ROOT).parts and '__pycache__' not in p.parts]
    patterns=[r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----',
              r'\bgh[pousr]_[A-Za-z0-9]{24,}\b',r'\bgithub_pat_[A-Za-z0-9_]{30,}\b',
              r'\bsk-(?:ant-)?[A-Za-z0-9_-]{24,}\b']
    python_files=0
    for source in files:
        text=source.read_text(encoding='utf-8')
        if source.suffix=='.py':
            ast.parse(text,filename=str(source));python_files+=1
        if source.suffix=='.json':json.loads(text)
        for pattern in patterns:
            assert not re.search(pattern,text),'Potential credential: '+str(source.relative_to(ROOT))
        if source.suffix=='.md':
            for target in re.findall(r'\]\(([^)]+)\)',text):
                if re.match(r'^(?:https?://|#)',target):continue
                assert not target.startswith('/mnt/'),'Private file link: '+str(source)
                assert (source.parent/target.split('#')[0]).exists(),(str(source),target)
    acceptance=json.loads((ROOT/'records/acceptance-2026-11-17.json').read_text())
    assert acceptance['real_claude_two_sessions']['all_passed']
    assert acceptance['regression']['tests']==108 and acceptance['regression']['result']=='OK'
    assert acceptance['persistent_project_environment']['passed']
    print(json.dumps({'release':manifest['release'],'runtime_files':len(manifest['files']),
                      'python_files_parsed':python_files,'archive_files':len(files),
                      'manifest_syntax_links_secret_patterns':'passed'},ensure_ascii=False))


if __name__=='__main__':main()
