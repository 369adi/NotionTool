"""Every writing coworker works in its own copy: no silent fallback into main."""
from types import SimpleNamespace

import pytest

from notiontool.bridge import Bridge
from notiontool.engine import task_is_read_only


def test_read_only_only_when_explicit():
    assert task_is_read_only({'read_only': True})
    assert task_is_read_only({'title': 'Read-only audit of the pool'})
    assert task_is_read_only({'prompt': 'Read-only: count the runs'})
    assert task_is_read_only({'prompt': '  read only check of x'})


def test_quoted_read_only_in_prompt_does_not_count():
    prompt = 'Merge these branches.\nContext: Read-only audit: recount every number fresh from the files.'
    assert not task_is_read_only({'title': 'Merge: coworker pool fixes', 'prompt': prompt})


def _bridge(patch_on):
    bridge = Bridge.__new__(Bridge)
    bridge.engine = SimpleNamespace(_patch_on=lambda run: patch_on)
    return bridge


def test_needs_own_copy_for_writing_executor_in_patch_mode():
    run = {'id': 'r1', 'project_path': 'x'}
    assert _bridge(True)._needs_own_copy({'role': 'executor', 'prompt': 'Fix it'}, run)
    assert not _bridge(False)._needs_own_copy({'role': 'executor', 'prompt': 'Fix it'}, run)
    assert not _bridge(True)._needs_own_copy({'role': 'reviewer', 'prompt': 'Review'}, run)
    assert not _bridge(True)._needs_own_copy({'role': 'executor', 'read_only': True}, run)
    assert not _bridge(True)._needs_own_copy({'role': 'executor'}, None)


def test_guard_scope_refuses_write_without_own_copy():
    bridge = _bridge(True)
    bridge.store = SimpleNamespace(put=lambda kind, doc: None)
    bridge.worktree = lambda task: None
    task = {'id': 't1', 'role': 'executor', 'prompt': 'Fix it', 'files': ['notiontool/a.py']}
    with pytest.raises(ValueError, match='no own copy'):
        bridge.guard_scope(task, {'id': 'r1', 'project_path': 'x'}, 'notiontool/a.py')
