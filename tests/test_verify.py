"""The import smoke check must fail CI when any dependency check fails."""
from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

import verify


def _fake_importer(module_name):
    attributes = {attribute: object() for _, imports in verify.CHECKS
                  for name, attribute in imports if name == module_name}
    return SimpleNamespace(**attributes)


def test_all_imports_succeed(monkeypatch, capsys):
    monkeypatch.setattr(verify, 'import_module', _fake_importer)
    assert verify.main() == 0
    output = capsys.readouterr().out
    assert output.count('[OK]') == len(verify.CHECKS)
    assert '[FAIL]' not in output


def test_one_failed_import_returns_nonzero_and_continues(monkeypatch, capsys):
    def import_with_failure(module_name):
        if module_name == 'backend.storage.db':
            raise ImportError('missing driver')
        return _fake_importer(module_name)

    monkeypatch.setattr(verify, 'import_module', import_with_failure)
    assert verify.main() == 1
    output = capsys.readouterr().out
    assert '[FAIL] backend.storage.db: missing driver' in output
    assert '[OK] agent.agent' in output  # Later checks still run.
    assert output.count('[OK]') == len(verify.CHECKS) - 1


def test_script_entrypoint_exits_with_failure(monkeypatch):
    monkeypatch.setattr('importlib.import_module', lambda name: (_ for _ in ()).throw(ImportError('broken')))
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(Path(verify.__file__)), run_name='__main__')
    assert exc.value.code == 1
