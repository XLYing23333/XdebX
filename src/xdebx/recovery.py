"""Durable installation journal and narrowly scoped crash recovery."""
import contextlib
import fcntl
import os
import shutil
import tempfile
import time
from pathlib import Path

from .runtime import InstallError, atomic_json, read_json, user_directory
from .i18n import tr


def no_symlinks(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise InstallError('Recovery path contains a symlink: ' + str(path))
    return path


def installation_running(ctx):
    path = ctx.state_dir / 'install.lock'
    if not path.exists():
        return False
    with path.open('r') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return False


class Journal:
    def __init__(self, ctx, runner, ordered, run_id, log_path):
        self.ctx, self.runner = ctx, runner
        self.path = ctx.state_dir / 'runs' / (run_id + '.json')
        self.work = ctx.cache_dir / 'work' / run_id
        self.record = {'schema_version': 2, 'run_id': run_id, 'target': ctx.target,
                       'ordered': ordered, 'current': None, 'phase': 'starting',
                       'active': True, 'results': [], 'log': str(log_path), 'artifacts': []}
        self.save()

    def save(self):
        atomic_json(self.path, self.record)
        self.ctx.fix_owner(self.path)

    def artifact(self, path, kind):
        item = {'path': str(Path(path).absolute()), 'kind': kind}
        if item not in self.record['artifacts']:
            self.record['artifacts'].append(item)
            self.save()

    def phase(self, key, phase):
        self.record.update(current=key, phase=phase)
        self.save()

    @contextlib.contextmanager
    def temporary(self, prefix='xdebx-'):
        no_symlinks(self.work)
        user_directory(self.ctx, self.work)
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(self.work)))
        self.record['artifacts'].append({'path': str(path), 'kind': 'temporary'})
        self.save()
        try:
            yield str(path)
        finally:
            if path.exists():
                no_symlinks(path)
                shutil.rmtree(str(path))

    def begin_component(self, target):
        target = no_symlinks(target)
        if target.exists():
            raise InstallError('Existing directory preserved; manual review needed: ' + str(target))
        if os.path.commonpath([str(target), str(self.ctx.home.absolute())]) != str(self.ctx.home.absolute()):
            raise InstallError('Managed installation outside user home')
        self.record['artifacts'].append({'path': str(target), 'kind': 'component'})
        self.save()
        user_directory(self.ctx, target.parent)
        target.mkdir()
        atomic_json(target / '.xdebx-owner.json', {'run_id': self.record['run_id'], 'complete': False})
        self.ctx.fix_owner(target)
        return target

    def complete_component(self, target):
        atomic_json(Path(target) / '.xdebx-owner.json', {'run_id': self.record['run_id'], 'complete': True})
        self.ctx.fix_owner(target)

    def install_tree(self, source, target):
        target = self.begin_component(target)
        shutil.copytree(str(source), str(target), symlinks=True, dirs_exist_ok=True)
        self.complete_component(target)

    def finish(self, results):
        pending = any(a.get('kind') == 'component' and
                      read_json(Path(a['path']) / '.xdebx-owner.json').get('complete') is False
                      for a in self.record['artifacts'])
        self.record.update(results=results, current=None, active=False, phase='finished', recovery_pending=pending)
        self.save()
        if self.work.exists():
            no_symlinks(self.work)
            shutil.rmtree(str(self.work))


def recover(ctx, emit):
    """Caller must hold install.lock. Preserve uncertain contents and user changes."""
    for path in sorted((ctx.state_dir / 'runs').glob('*.json')):
        record = read_json(path)
        if not record.get('active') and not record.get('recovery_pending'):
            continue
        run_id = record.get('run_id', '')
        if not run_id.isdigit():
            emit('Warning: invalid interrupted journal preserved: ' + str(path))
            continue
        for artifact in record.get('artifacts', []):
            candidate = Path(artifact.get('path', ''))
            try:
                candidate = no_symlinks(candidate)
                if not candidate.exists():
                    continue
                work = no_symlinks(ctx.cache_dir / 'work' / run_id)
                if artifact.get('kind') == 'temporary' and candidate.parent == work:
                    shutil.rmtree(str(candidate))
                    emit(tr(ctx, "recovery_temp", path=str(candidate)))
                elif artifact.get('kind') == 'download-part' and candidate.parent == (ctx.cache_dir / 'downloads').absolute() and candidate.name.endswith('.part'):
                    candidate.unlink()
                    emit(tr(ctx, "recovery_temp", path=str(candidate)))
                elif artifact.get('kind') == 'apt-directory' and candidate.parent == Path('/tmp') and candidate.name.startswith(('xdebx-apt-', 'xdebx-repo-', 'xdebx-source-')):
                    marker = candidate / '.xdebx-owner.json'
                    no_symlinks(marker)
                    if read_json(marker).get('run_id') != run_id:
                        raise InstallError('APT directory ownership not confirmed')
                    shutil.rmtree(str(candidate))
                    emit(tr(ctx, "recovery_temp", path=str(candidate)))
                elif artifact.get('kind') == 'component':
                    root = ctx.home.absolute()
                    if os.path.commonpath([str(root), str(candidate)]) != str(root) or candidate == root:
                        raise InstallError('Component outside user home')
                    marker = candidate / '.xdebx-owner.json'
                    no_symlinks(marker)
                    owner = read_json(marker)
                    if owner.get('run_id') != run_id or owner.get('complete') is not False:
                        emit(tr(ctx, "recovery_preserved", path=str(candidate)))
                        continue
                    # Quarantine instead of deleting: even incomplete trees may contain user edits.
                    backup = ctx.backups / ('recovery-' + str(time.time_ns()))
                    no_symlinks(backup)
                    user_directory(ctx, backup)
                    shutil.move(str(candidate), str(backup / 'incomplete-component'))
                    atomic_json(backup / 'recovery.json', {'destination': str(candidate), 'run_id': run_id})
                    ctx.fix_owner(backup)
                    emit(tr(ctx, "recovery_backup", path=str(backup)))
            except (InstallError, OSError, ValueError) as exc:
                emit(tr(ctx, "recovery_warning", error=str(exc)))
        work = ctx.cache_dir / 'work' / run_id
        if work.exists():
            try:
                no_symlinks(work)
                work.rmdir()  # Never recursively remove unrecorded contents.
            except (InstallError, OSError):
                pass
        current = record.get('current')
        if current and current not in {r.get('id') for r in record.get('results', [])}:
            record.setdefault('results', []).append({'id': current, 'status': 'interrupted',
                                                     'method': record.get('phase'), 'attempts': []})
        record.update(active=False, current=None, recovery_pending=False,
                      phase='interrupted' if record.get('active') else 'recovered')
        atomic_json(path, record)
        ctx.fix_owner(path)
