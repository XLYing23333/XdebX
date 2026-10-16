#!/usr/bin/env python3
"""Small real installation plus crash recovery, only inside a disposable container."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from xdebx.installer import Installer
from xdebx.recovery import Journal
from xdebx.runtime import Context, atomic_json, user_directory


def main():
    if not Path('/.dockerenv').exists():
        raise SystemExit('This test runs only inside a disposable Docker container')
    ctx = Context('xdebx', 'no')
    engine = Installer(ctx, lambda text: print(text, flush=True))
    # Compatibility tests must exercise the current recipe rather than old acceptance gates.
    engine.resources.ignore_compatibility = True
    user_directory(ctx, ctx.state_dir)
    config = ctx.home / '.vimrc'; config.write_bytes(b'" preserved user configuration\n')
    ctx.fix_owner(config); before = config.read_bytes()
    journal = Journal(ctx, engine.runner, ['nodejs22-nvm'], '123456789', ctx.state_dir / 'old.log')
    journal.phase('nodejs22-nvm', 'official')
    work = journal.work / 'unpack'; user_directory(ctx, work); (work / 'partial').write_text('partial download')
    component = ctx.home / '.nvm'; user_directory(ctx, component)
    (component / 'user-note').write_text('preserve even interrupted user edits')
    atomic_json(component / '.xdebx-owner.json', {'run_id': '123456789', 'complete': False})
    journal.record['artifacts'] = [{'path': str(work), 'kind': 'temporary'}, {'path': str(component), 'kind': 'component'}]
    journal.save()
    try:
        results = engine.execute(['htop', 'curl'])
        assert all(r['status'] in ('success', 'installed') for r in results), results
        assert engine.detected('htop')['status'] == 'installed'
        assert engine.detected('curl')['status'] == 'installed'
        assert not work.exists() and not component.exists()
        assert next(ctx.backups.glob('recovery-*/incomplete-component/user-note')).read_text().startswith('preserve')
        assert config.read_bytes() == before
        retry = engine.execute(['htop'])
        assert retry[0]['status'] == 'installed', retry
        from container_case import tui
        tui_result = tui(engine)
        assert tui_result['status'] == 'passed'
        outcome = {'target': ctx.target, 'status': 'passed', 'installation': results,
                   'checks': ['actual APT installation and probes', 'crashed journal recovery',
                              'scoped temporary cleanup', 'incomplete component quarantine',
                              'configuration unchanged', 'healthy software skipped on retry',
                              'normal/lite TUI, resize and clean quit']}
        Path(sys.argv[1]).write_text(json.dumps(outcome, indent=2) + '\n')
    finally:
        engine.close()


if __name__ == '__main__':
    main()
