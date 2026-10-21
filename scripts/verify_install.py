"""Exercise the installed distribution outside the checkout, denying package writes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

CODE = r"""
import hashlib, json, os, sys
from pathlib import Path
import miexiang_publish
from miexiang_publish import paths
from miexiang_publish.cli import main
from miexiang_publish.models import identify_artifact
from miexiang_publish.staging import stage_snapshot
from miexiang_publish.locking import IdentityLock
package = Path(miexiang_publish.__file__).resolve().parent
assert 'site-packages' in package.parts, 'must install a real wheel, not use source/editable mode'
assert not package.is_relative_to(Path(sys.argv[1]).resolve() / 'src')
before = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest() for p in package.rglob('*') if p.is_file()}
def guard(event, args):
    if event == 'open':
        target, mode, flags = args
        if isinstance(target, (str, bytes)):
            target = Path(os.fsdecode(target)).absolute()
            writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if writing and target.is_relative_to(package):
                raise PermissionError('test denies package writes')
    if event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.rename'}:
        for target in args[:2] if event == 'os.rename' else args[:1]:
            if isinstance(target, (str, bytes)) and Path(os.fsdecode(target)).absolute().is_relative_to(package):
                raise PermissionError('test denies package mutation')
sys.addaudithook(guard)
try:
    (package / 'write-must-fail').write_text('probe')
except PermissionError:
    pass
else:
    raise AssertionError('package write guard not active')
root = Path.cwd() / '数据 with spaces'
paths.configure_runtime(root)
source = Path.cwd() / '原始 fixture.bin'
source.write_bytes(b'installed wheel smoke')
artifact = identify_artifact(source, 'fixture', '1.0', 'test')
with IdentityLock('fixture', 'test', '1.0'):
    staged = stage_snapshot(artifact)
    assert staged.source_path.is_relative_to(root)
    assert staged.source_path.read_bytes() == source.read_bytes()
    staged.source_path.unlink()
assert main(['--data-dir', str(root), 'offline-demo']) == 0
after = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest() for p in package.rglob('*') if p.is_file()}
assert before == after
print(json.dumps({'INSTALL_SMOKE':'PASS','PACKAGE_WRITES':'DENIED_BY_AUDIT_HOOK','PACKAGE_BYTES_UNCHANGED':True}))
"""
root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='installed-smoke-') as tmp:
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-I', '-B', '-c', CODE, str(root)], cwd=tmp, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    print(result.stdout, end='')
    if result.returncode:
        # Private opt-in diagnostics; never echo provider/local detail to public logs.
        if env.get('MIEXIANG_INSTALL_DIAGNOSTIC'):
            Path(env['MIEXIANG_INSTALL_DIAGNOSTIC']).write_text(result.stderr, encoding='utf-8')
        print(json.dumps({'INSTALL_SMOKE':'FAIL','DETAIL':'private subprocess diagnostics withheld'}))
        sys.exit(result.returncode)
