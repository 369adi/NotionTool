import json, sqlite3

from notiontool import test_app
from notiontool.devtools import NAMES


def test_tool_is_registered_for_operator_and_coworkers():
    assert 'nt_test_app' in NAMES


def test_startup_errors_reads_failure_events(tmp_path):
    con = sqlite3.connect(str(tmp_path / 'notiontool.db'))
    con.execute('CREATE TABLE events(seq INTEGER, ts REAL, run_id TEXT, kind TEXT, doc TEXT)')
    con.execute("INSERT INTO events VALUES (1, 0, '', 'test_instance_started', '{}')")
    con.execute("INSERT INTO events VALUES (2, 0, '', 'desktop_app_failed', ?)", (json.dumps({'message': 'boom'}),))
    con.commit(); con.close()
    assert test_app.startup_errors(tmp_path) == ['desktop_app_failed: boom']
    assert test_app.startup_errors(tmp_path / 'missing') == []


def test_bad_action_is_refused():
    try:
        test_app.run('explode')
    except ValueError as exc:
        assert 'action' in str(exc)
    else:
        raise AssertionError('expected ValueError')
