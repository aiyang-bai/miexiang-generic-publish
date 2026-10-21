"""Bounded private maintenance guard; never edits source or performs remote operations."""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, encoding='utf-8').strip()


def process_creation():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class FILETIME(ctypes.Structure):
            _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(FILETIME)] * 4)]
        times = [FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(x) for x in times)):
            raise RuntimeError('cannot establish process creation identity')
        return {'windows_filetime': (times[0].high << 32) | times[0].low}
    # fcntl keeps lock ownership in the OS; this observation is not a PID-reuse check.
    return {'observed_process_start_unix': time.time(), 'native_creation_time': 'NOT_RUN'}


def atomic_json(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--guard', action='store_true')
    mode.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    common = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    canonical = common.parent
    state_dir = canonical / '.maintenance'
    if state_dir.is_symlink() or (state_dir.exists() and getattr(state_dir.lstat(), 'st_file_attributes', 0) & 0x400):
        raise RuntimeError('private state may not be a link')
    registry = json.loads((state_dir / 'repository.json').read_text(encoding='utf-8'))
    if canonical != Path(registry['canonical']).resolve() or root not in [Path(p).resolve() for p in registry['worktrees']]:
        raise RuntimeError('unregistered maintenance worktree; canonical source is read-only for maintenance')
    # Keep one byte for Windows range locking. Never unlink this shared file.
    lock = (state_dir / 'run.lock').open('a+b')
    lock.seek(0, 2)
    if lock.tell() == 0:
        lock.write(b'0'); lock.flush()
    lock.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({'status': 'SKIPPED_ALREADY_RUNNING'}), flush=True)
        lock.close()
        return 0
    run_id = uuid.uuid4().hex
    now = datetime.now(timezone(timedelta(hours=8)))
    receipt = {'run_id':run_id, 'pid':os.getpid(), 'process_creation':process_creation(), 'started':now.isoformat(), 'mode':'weekly' if now.weekday() == 5 else 'daily', 'base_commit':git(root,'rev-parse','HEAD'), 'initial_diff':git(root,'status','--porcelain=v1'), 'worktree':str(root), 'status':'RUNNING'}
    try:
        previous = json.loads((state_dir/'state.json').read_text(encoding='utf-8')) if (state_dir/'state.json').exists() else {}
        receipt['previous_status'] = previous.get('status', 'NONE')
        atomic_json(state_dir/'active.json', receipt)
        print(json.dumps(receipt, ensure_ascii=False), flush=True)
        if args.guard:
            line = sys.stdin.readline()
            if not line:
                raise RuntimeError('guard lost input; session incomplete')
            completed = json.loads(line)
            if completed.get('status') not in {'NO_CHANGE','CHANGED','WAITING_EXTERNAL','FAIL'}:
                raise RuntimeError('invalid completion status')
            receipt.update({k:completed[k] for k in ('status','summary','tests','blockers','next') if k in completed})
        else:
            if args.verify:
                env = dict(os.environ, MIEXIANG_PUBLISH_DATA_DIR=str(state_dir/'rehearsal-data'), TEMP=str(state_dir), TMP=str(state_dir))
                result = subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
                (state_dir/f'{run_id}-tests.log').write_text(result.stdout+result.stderr,encoding='utf-8')
                receipt['tests']={'exit_code':result.returncode,'log':f'{run_id}-tests.log'}
                if result.returncode:
                    raise RuntimeError('rehearsal tests failed')
            receipt.update(status='NO_CHANGE',summary='Local inspection/checks only; no source or remote mutation. Agent must separately inspect backlog under --guard for full maintenance.')
        receipt['result_commit'] = git(root,'rev-parse','HEAD')
        receipt['final_diff'] = git(root,'status','--porcelain=v1')
        receipt['finished'] = datetime.now(timezone.utc).isoformat()
        atomic_json(state_dir/'state.json',receipt)
        atomic_json(state_dir/f'{run_id}-receipt.json',receipt)
        print(json.dumps(receipt,ensure_ascii=False),flush=True)
        return 0
    except Exception:
        receipt.update(status='FAIL',summary='Maintenance interrupted or failed; inspect private inputs/check logs before retrying.')
        atomic_json(state_dir/'state.json',receipt)
        atomic_json(state_dir/f'{run_id}-receipt.json',receipt)
        raise
    finally:
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_UN)
        lock.close()

if __name__ == '__main__':
    raise SystemExit(main())
