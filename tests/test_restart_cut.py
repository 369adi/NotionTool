from datetime import datetime, timezone
import threading

from notiontool import turn_watch as tw


def iso(sec):
    return datetime.fromtimestamp(sec, timezone.utc).isoformat().replace('+00:00', 'Z')


START = 1_792_632_388.0  # 22.10. 03:26:28 (live restart)


def transcript(entries, status='idle'):
    return {'session': {'status': status}, 'patches': [{'op': 'put', 'entity': e} for e in entries]}


def tool(seq, at, done):
    return {'id': 'tool-%d' % seq, 'sequence': seq, 'kind': 'tool', 'tool_use_id': 'u%d' % seq,
            'created_at': iso(at), 'result': {'completed_at': iso(done), 'is_error': False}}


def user(seq, at, text='mach weiter'):
    return {'id': 'user-%d' % seq, 'sequence': seq, 'kind': 'user_message', 'created_at': iso(at), 'content': text}


def assistant(seq, at):
    return {'id': 'a-%d' % seq, 'sequence': seq, 'kind': 'assistant_message', 'created_at': iso(at)}


def test_stamp_reads_iso_strings():
    assert abs(tw.stamp({'created_at': iso(START)}) - START) < 0.01
    assert tw.stamp({'created_at': START * 1000}) == START
    assert tw.stamp({'created_at': 'garbage'}) == 0.0


def test_live_case_after_restart_is_a_cut():
    # 03:26:56 tool started, 03:26:58 result, then silence: the dropped turn from the live transcript.
    t = transcript([user(1, START - 400), tool(2, START + 28, START + 30)])
    hit = tw.restart_cut(t, START, START + 90)
    assert hit and hit['key'] == 'restartcut:tool-2'


def test_cut_shortly_before_restart():
    # 22:13:44 result, restart 45 s later.
    t = transcript([user(1, START - 300), tool(2, START - 47, START - 45)])
    assert tw.restart_cut(t, START, START + 60)


def test_no_cut_when_finished_running_old_or_fresh():
    assert tw.restart_cut(transcript([user(1, START - 60), assistant(2, START - 10)]), START, START + 90) is None
    assert tw.restart_cut(transcript([tool(2, START, START + 5)], status='running'), START, START + 90) is None
    assert tw.restart_cut(transcript([tool(2, START - 900, START - 899)]), START, START + 90) is None
    assert tw.restart_cut(transcript([tool(2, START + 50, START + 80)]), START, START + 90) is None  # quiet < 30 s


class FakeClient:
    info = {'space_id': 'space'}

    def __init__(self, transcripts):
        self.transcripts = transcripts

    def threads(self):
        return [{'id': tid, 'guessed': True} for tid in self.transcripts]

    def transcript(self, tid, limit=100):
        return self.transcripts[tid]


class FakeStore:
    def __init__(self):
        self.objects = {('accounts', 'acc'): {'id': 'acc', 'state': 'ready'}}
        self.events = []

    def settings(self):
        return {}

    def get(self, kind, ident, default=None):
        return self.objects.get((kind, ident), default)

    def all(self, kind):
        return [v for (k, _), v in self.objects.items() if k == kind]

    def put(self, kind, doc):
        self.objects[(kind, doc['id'])] = doc

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))


class FakeAdapter:
    def __init__(self, client):
        self.c, self.sent = client, []

    def client(self, account_id):
        return self.c

    def new_id(self, space, tag):
        return 'step-%d' % len(self.sent)

    def continue_turn(self, worker, text, step):
        self.sent.append((worker['thread_id'], worker['purpose'], text))
        return True


class FakeEngine:
    lock = threading.RLock()
    stop_event = threading.Event()


def test_scan_continues_every_dropped_chat_once(monkeypatch):
    monkeypatch.setattr(tw, 'budget_used_up', lambda store, now: False)
    client = FakeClient({
        'roblox': transcript([user(1, START - 200), tool(2, START + 20, START + 25)]),
        'sonstiges': transcript([user(1, START - 100), tool(2, START - 40, START - 38)]),
        'done': transcript([user(1, START - 100), assistant(2, START - 50)]),
    })
    adapter, store = FakeAdapter(client), FakeStore()
    clock = [START + 120]
    watch = tw.TurnWatch(store, adapter, FakeEngine(), clock=lambda: clock[0])
    watch.started = START
    watch.spawn = lambda fn, *args: fn(*args)
    assert sorted(watch.scan_restart_cuts()) == ['roblox', 'sonstiges']
    assert sorted(t for t, _, _ in adapter.sent) == ['roblox', 'sonstiges']
    assert all(p == 'external' and text == tw.RESTART_CUT_TEXT for _, p, text in adapter.sent)
    assert watch.scan_restart_cuts() == []  # once per dropped turn
    clock[0] = START + tw.RESTART_SCAN_END + 1
    assert watch.scan_restart_cuts() == []
