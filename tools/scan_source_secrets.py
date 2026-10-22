"""Redacting scan of staged text and forbidden binary/runtime paths.

Heuristic source scan, not proof of absence. Prints rules/paths/lines, never values.
"""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

RULES = {
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github_token': re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}'),
    'aws_access_key': re.compile(rb'AKIA[0-9A-Z]{16}'),
    'provider_secret_key': re.compile(rb'sb_secret_[A-Za-z0-9_-]{25,}|sk-(?:proj-)?[A-Za-z0-9_-]{40,}'),
    'jwt_value': re.compile(rb'eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'),
    'telegram_bot_token': re.compile(rb'\b[0-9]{8,12}:[A-Za-z0-9_-]{35}\b'),
    'database_password_url': re.compile(rb'postgres(?:ql)?://[^\s:/\x22\x27]+:[^\s@/\x22\x27]{8,}@'),
}
FORBIDDEN = {'.mp4','.mov','.avi','.webm','.ogv','.safetensors','.pt','.pth','.onnx','.db','.sqlite3','.log'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged',action='store_true')
    args=parser.parse_args()
    command=['git','diff','--cached','--name-only','-z'] if args.staged else ['git','ls-files','--cached','--others','--exclude-standard','-z']
    paths=[Path(x.decode()) for x in subprocess.check_output(command).split(b'\0') if x]
    findings=[];scanned=0;allowed=0
    allowlist=json.loads((Path(__file__).parent/'secret_scan_allowlist.json').read_text())
    for path in paths:
        if not path.is_file():continue
        if path.suffix.lower() in FORBIDDEN or any(p in {'data','test-data','checkpoints','.offline-demo','.venv','node_modules','.impeccable'} for p in path.parts) or (str(path).startswith(('docs/engineering/', 'docs/ai/')) and path.name.endswith('_RESULTS.json')):
            findings.append({'path':str(path),'rule':'runtime_or_private_asset'});continue
        if path.name.startswith('.env') and path.name not in {'.env.example','.env.online.example'}:
            findings.append({'path':str(path),'rule':'private_environment'});continue
        data=path.read_bytes()
        if b'\0' in data:continue
        scanned+=1
        for rule,pattern in RULES.items():
            for match in pattern.finditer(data):
                if any(a['path']==str(path) and a['rule']==rule and a['sha256']==hashlib.sha256(match.group()).hexdigest() for a in allowlist):
                    allowed+=1
                    continue
                findings.append({'path':str(path),'line':data[:match.start()].count(b'\n')+1,'rule':rule})
    print(json.dumps({'status':'FAIL' if findings else 'PASS','scope':'staged' if args.staged else 'nonignored source','text_files_scanned':scanned,'reviewed_disposable_fixture_matches':allowed,'findings':findings,'limits':'Heuristic patterns; images and unknown secret formats require review'},indent=2))
    return 1 if findings else 0


if __name__=='__main__':raise SystemExit(main())
