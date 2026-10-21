"""Scan the actual tracked tree and built members; never scan credential stores."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    'private_key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github_token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'),
    'private_machine': re.compile(rb'(?:[A-Za-z]:[\\/](?:SoftwareData|Users[\\/]888)|aiyang-bai[/]miexiang-publish-test|127\.0\.0\.1:8787)', re.I),
}
FORBIDDEN = {'.maintenance', 'AUTOPILOT', '.git', '.venv', 'runtime', 'research', 'cache', '__pycache__'}
# Explicit synthetic redaction input, never an operational token.
SYNTHETIC = {'tests/test_contract.py': {b'ghp_' + b'123456789012345678901234567890123456'}}

def check(name, data, issues):
    parts = PurePosixPath(name).parts
    if FORBIDDEN.intersection(parts) or name.endswith(('.pyc', '.pyo')) or 'profile.json' == PurePosixPath(name).name:
        issues.append({'file': name, 'rule': 'forbidden_member'})
    for rule, pattern in RULES.items():
        for match in pattern.finditer(data):
            synthetic_name = next((key for key in SYNTHETIC if name == key or name.endswith('/' + key)), '')
            if match.group() not in SYNTHETIC.get(synthetic_name, set()):
                issues.append({'file': name, 'rule': rule, 'line': data[:match.start()].count(b'\n') + 1})


def audit():
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode('utf-8').split('\0')
    issues, tracked, packages = [], [], []
    git_blob_checks = 0
    for line in subprocess.check_output(["git", "ls-files", "--stage"], cwd=ROOT, text=True, encoding="utf-8").splitlines():
        metadata, name = line.split("\t", 1)
        mode, oid, stage = metadata.split()
        if stage != "0" or mode not in {"100644", "100755"}:
            issues.append({"file":name,"rule":"unsupported_index_entry"})
        else:
            check(name, subprocess.check_output(["git", "cat-file", "blob", oid], cwd=ROOT), issues)
            git_blob_checks += 1
    commits = subprocess.check_output(["git", "rev-list", "--all"], cwd=ROOT, text=True).splitlines()
    for commit in commits:
        tree = subprocess.check_output(["git", "ls-tree", "-r", "-z", commit], cwd=ROOT).decode("utf-8")
        for entry in filter(None, tree.split("\0")):
            metadata, name = entry.split("\t", 1)
            mode, kind, oid = metadata.split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                issues.append({"file":name,"rule":"unsupported_history_entry"})
            else:
                check(name, subprocess.check_output(["git", "cat-file", "blob", oid], cwd=ROOT), issues)
                git_blob_checks += 1
    for name in filter(None, names):
        data = (ROOT / name).read_bytes()
        check(name, data, issues)
        tracked.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
    for archive in sorted((ROOT / 'dist').glob('*')):
        if archive.suffix == '.whl' or archive.suffix == '.zip':
            with zipfile.ZipFile(archive) as z:
                members = [(n, z.read(n)) for n in z.namelist() if not n.endswith('/')]
        elif archive.name.endswith('.tar.gz'):
            with tarfile.open(archive) as t:
                members = [(m.name, t.extractfile(m).read()) for m in t.getmembers() if m.isfile()]
        else:
            continue
        for name, data in members:
            check(name, data, issues)
        packages.append({'name': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'size': archive.stat().st_size, 'members': [n for n, _ in members]})
    if not tracked or not packages:
        issues.append({'rule': 'missing_tracked_tree_or_packages'})
    result = {'status': 'PASS' if not issues else 'FAIL', 'scope': 'tracked files and built members only; pattern scan plus maintainer review, not a proof of universal secret absence', 'synthetic_exceptions': {k: len(v) for k, v in SYNTHETIC.items()}, 'git_blob_checks': git_blob_checks, 'commits_scanned': len(commits), 'tracked': tracked, 'packages': packages, 'issues': issues}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not issues else 1

if __name__ == '__main__':
    raise SystemExit(audit())
