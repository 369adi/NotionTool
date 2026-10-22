"""A coworker whose chat cannot reach the project is taken out of the pool; its task goes to another one once."""
from types import SimpleNamespace

from notiontool.engine import Engine, reports_no_tool_access


def test_detects_the_real_replies():
    assert reports_no_tool_access("I couldn't do this part. I have no access to your worktree, so I couldn't read the branch")
    assert reports_no_tool_access('I have no access to your project folder')
    assert reports_no_tool_access('No NotionTool tools are available in this chat')


def test_normal_results_do_not_count():
    assert not reports_no_tool_access('Merged the branch; all 29 tests pass.')
    assert not reports_no_tool_access('The guard now refuses writes with no access to main for read-only roles.')


class _Store:
    def __init__(self, workers):
        self.workers = workers
        self.events = []

    def get(self, kind, ident, default=None):
        return self.workers.get(ident, default)

    def put(self, kind, doc):
        self.workers[doc['id']] = doc

    def event(self, name, run_id, **fields):
        self.events.append(name)


def _engine(store):
    engine = Engine.__new__(Engine)
    engine.store = store
    return engine


def test_first_miss_keeps_the_chat_and_retries_the_task():
    store = _Store({'w1': {'id': 'w1', 'purpose': 'worker', 'enabled': True}})
    engine = _engine(store)
    task = {'id': 't1', 'run_id': 'r1', 'state': 'done', 'attempts': 1}
    engine._no_tool_access(task, 'w1')
    assert store.workers['w1']['enabled'] is True  # Notion AI often just skipped the tool load once
    assert store.workers['w1']['no_access_strikes'] == 1
    assert task['state'] == 'retry' and task['attempts'] == 0
    assert store.events == ['coworker_no_tool_access']


def test_second_miss_in_a_row_takes_the_chat_out():
    store = _Store({'w1': {'id': 'w1', 'purpose': 'worker', 'enabled': True}})
    engine = _engine(store)
    task = {'id': 't1', 'run_id': 'r1', 'state': 'done', 'attempts': 1}
    engine._no_tool_access(task, 'w1')
    task['state'] = 'done'
    engine._no_tool_access(task, 'w1')
    assert store.workers['w1']['enabled'] is False
    assert store.workers['w1']['tool_access_lost']
    assert task['state'] == 'done'  # the task itself is retried only once: no endless loop


def test_success_clears_the_strikes():
    store = _Store({'w1': {'id': 'w1', 'purpose': 'worker', 'enabled': True, 'no_access_strikes': 1}})
    engine = _engine(store)
    engine._tool_access_ok('w1')
    assert store.workers['w1']['no_access_strikes'] == 0
    engine._no_tool_access({'id': 't2', 'run_id': 'r1', 'state': 'done', 'attempts': 1}, 'w1')
    assert store.workers['w1']['enabled'] is True


def test_main_chat_is_never_disabled():
    store = _Store({'m': {'id': 'm', 'purpose': 'operator', 'enabled': True}})
    _engine(store)._no_tool_access({'id': 't', 'run_id': 'r', 'state': 'done', 'attempts': 1}, 'm')
    assert store.workers['m']['enabled'] is True
