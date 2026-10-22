from notiontool import main_wake as W


RUNS = {'r1': {'id': 'r1', 'operator': True, 'main_worker': 'acc:mainthread'}, 'r2': {'id': 'r2'}}


def task(**kw):
    base = dict(id='t1', run_id='r1', state='uncertain', reconcile_checks=W.STUCK_CHECKS,
                worker_id='acc:coworker9thread', title='Fix 2', error='Awaiting confirmation')
    base.update(kw)
    return base


def test_unconfirmed_task_gets_one_alarm():
    assert [t['id'] for _, t in W.stuck_tasks([task()], RUNS, set())] == ['t1']
    assert W.stuck_tasks([task()], RUNS, {'t1'}) == []  # already reported


def test_no_alarm_while_working_early_or_not_operator():
    assert W.stuck_tasks([task(remote_working=True)], RUNS, set()) == []
    assert W.stuck_tasks([task(reconcile_checks=W.STUCK_CHECKS - 1)], RUNS, set()) == []
    assert W.stuck_tasks([task(state='done')], RUNS, set()) == []
    assert W.stuck_tasks([task(run_id='r2')], RUNS, set()) == []


def test_message_names_task_and_next_step():
    text = W.build_stuck_message(W.stuck_tasks([task()], RUNS, set()))
    assert text.startswith(W.STUCK_HEAD) and 't1' in text and 'Fix 2' in text and 'never resend' in text


class Store:
    def __init__(self, tasks):
        self.data = {'tasks': tasks, 'runs': list(RUNS.values()), W.STUCK_KIND: []}
        self.events = []

    def all(self, kind):
        return list(self.data.get(kind, []))

    def get(self, kind, key, default=None):
        return next((r for r in self.data.get(kind, []) if r.get('id') == key), default)

    def put(self, kind, record):
        rows = [r for r in self.data.setdefault(kind, []) if r.get('id') != record['id']]
        self.data[kind] = rows + [record]

    def event(self, kind, **kw):
        self.events.append(kind)


class Client:
    def __init__(self, busy):
        self.busy = busy

    def transcript(self, thread, limit=100):
        return {'session': {'status': 'running' if self.busy else 'idle'}}


class Adapter:
    def __init__(self, busy):
        self.c = Client(busy)

    def client(self, account):
        return self.c


def make(busy):
    store = Store([task()])
    wake = W.MainWake(store, Adapter(busy), engine=None, clock=lambda: 1000.0)
    wake.sent_texts = []
    wake._send = lambda client, thread, text: wake.sent_texts.append((thread, text))
    return store, wake


def test_alarm_sent_once_to_the_main_chat():
    store, wake = make(busy=False)
    assert wake.stuck_tick(1000.0) == ['t1']
    assert wake.sent_texts[0][0] == 'mainthread'
    assert 'stuck_alert_sent' in store.events
    assert wake.stuck_tick(5000.0) == []  # never twice


def test_alarm_waits_while_main_is_busy():
    store, wake = make(busy=True)
    assert wake.stuck_tick(1000.0) == [] and wake.sent_texts == []
    assert store.all(W.STUCK_KIND) == []  # not recorded, so it is sent once the Main is idle
