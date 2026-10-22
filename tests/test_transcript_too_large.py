from notiontool.notion import NotionClient


class Fake:
    info = {'space_id': 's'}

    def __init__(self, max_ok):
        self.max_ok = max_ok
        self.limits = []

    def call(self, name, body):
        self.limits.append(body['limit'])
        if body['limit'] > self.max_ok:
            raise RuntimeError('NotionStreamError: Edge request interrupted; reconcile before reuse (Error: Response too large)')
        return {'session': {'status': 'idle'}, 'limit': body['limit']}


def test_too_large_reads_a_smaller_window():
    f = Fake(max_ok=10)
    out = NotionClient.transcript(f, 't', limit=100)
    assert out['limit'] == 6 and f.limits == [100, 25, 6]


def test_too_large_even_at_one_entry_raises():
    f = Fake(max_ok=0)
    try:
        NotionClient.transcript(f, 't', limit=40)
    except RuntimeError as exc:
        assert 'Response too large' in str(exc)
    else:
        raise AssertionError('expected the error')
    assert f.limits == [40, 10, 2, 1]


def test_other_errors_are_not_retried():
    class Boom(Fake):
        def call(self, name, body):
            self.limits.append(body['limit'])
            raise RuntimeError('HTTP 500')
    f = Boom(max_ok=100)
    try:
        NotionClient.transcript(f, 't', limit=30)
    except RuntimeError:
        pass
    assert f.limits == [30]
