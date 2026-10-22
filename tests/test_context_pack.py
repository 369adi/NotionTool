import os

from notiontool import context_pack as cp


def _make(tmp_path, name, count):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join('line %d' % i for i in range(1, count + 1)), encoding='utf-8')
    return name


def test_range_marker_inserts_numbered_lines(tmp_path):
    _make(tmp_path, 'pkg/a.py', 50)
    out, info = cp.expand('Fix it.\n[[code: pkg/a.py:10-12]]', [], str(tmp_path))
    assert 'File pkg/a.py lines 10-12 of 50' in out
    assert '10| line 10' in out and '12| line 12' in out and 'line 13' not in out
    assert info['added'] and info['blocks'] == 1 and cp.NOTE in out


def test_whole_file_marker_and_too_long_pointer(tmp_path):
    _make(tmp_path, 'small.py', 5)
    _make(tmp_path, 'big.py', cp.WHOLE_MAX_LINES + 1)
    out, info = cp.expand('[[code: small.py]] [[code: big.py]]', [], str(tmp_path))
    assert '5| line 5' in out
    assert 'too long to include whole' in out
    assert info['blocks'] == 1


def test_small_scope_files_added_automatically_big_ones_not(tmp_path):
    _make(tmp_path, 's.py', 3)
    _make(tmp_path, 'b.py', cp.AUTO_MAX_LINES + 1)
    out, info = cp.expand('Task', ['s.py', 'b.py'], str(tmp_path))
    assert 'File s.py lines 1-3' in out and 'b.py' not in out
    assert info['blocks'] == 1


def test_no_duplicate_when_marker_and_files_name_same_file(tmp_path):
    _make(tmp_path, 's.py', 3)
    out, info = cp.expand('[[code: s.py:1-2]]', ['s.py'], str(tmp_path))
    assert out.count('File s.py') == 1


def test_outside_root_and_missing_are_refused(tmp_path):
    out, info = cp.expand('[[code: ../secret.txt]] [[code: nope.py]]', ['../x.py'], str(tmp_path))
    assert out.count('not found in the project folder') == 2
    assert not info['blocks']


def test_budget_skips_and_test_marker(tmp_path):
    _make(tmp_path, 'a.py', 100)
    out, info = cp.expand('[[code: a.py]] [[test: python -m pytest tests -q]]', [], str(tmp_path), budget=50)
    assert 'budget full' in out and info['skipped'] == 1
    assert 'run exactly `python -m pytest tests -q`' in out
    assert info['added'] and cp.NOTE not in out


def test_plain_prompt_unchanged(tmp_path):
    out, info = cp.expand('Just do X.', [], str(tmp_path))
    assert out == 'Just do X.' and not info['added']
