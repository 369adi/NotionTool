"""New chats in the standalone app: Notion stores a createThread send under its own id."""
from notiontool import app_routes


class FakeApi:
    def __init__(self, chats, transcripts):
        self._chats, self._transcripts, self.calls = chats, transcripts, 0

    def chats(self, known=()):
        self.calls += 1
        return 'space', list(self._chats)

    def chat(self, tid, limit):
        if tid not in self._transcripts:
            raise RuntimeError('no such thread')
        return {'messages': self._transcripts[tid]}


def test_find_created_picks_the_chat_with_the_prompt():
    api = FakeApi(
        [{'id': 'old', 'updated': 1000}, {'id': 'other', 'updated': 99000}, {'id': 'real', 'updated': 100000}],
        {'other': [{'role': 'user', 'text': 'something else'}],
         'real': [{'role': 'user', 'text': 'Hallo'}, {'role': 'assistant', 'text': 'Hi'}],
         'old': [{'role': 'user', 'text': 'Hallo'}]})
    assert app_routes._find_created(api, 'mine', 'Hallo', 98.0, tries=1, pause=0) == 'real'


def test_find_created_keeps_our_id_when_it_holds_the_chat():
    api = FakeApi([], {'mine': [{'role': 'user', 'text': 'Hallo'}]})
    assert app_routes._find_created(api, 'mine', 'Hallo', 98.0, tries=1, pause=0) == ''
    assert api.calls == 0


def test_find_created_gives_up_without_evidence():
    api = FakeApi([{'id': 'x', 'updated': 100000}], {'x': [{'role': 'user', 'text': 'nope'}]})
    assert app_routes._find_created(api, 'mine', 'Hallo', 98.0, tries=2, pause=0) == ''
    assert api.calls == 2


def test_job_unwraps_nested_and_job_id_shapes():
    assert app_routes._job({'job': {'id': 'j1', 'title': 'Reload workspace', 'state': 'queued'}}) == \
        {'id': 'j1', 'title': 'Reload workspace', 'state': 'queued'}
    assert app_routes._job({'ok': True, 'job_id': 'j2'}) == {'ok': True, 'id': 'j2'}
    assert app_routes._job({'id': 'j3', 'state': 'done', 'extra': 1}) == {'id': 'j3', 'state': 'done'}
