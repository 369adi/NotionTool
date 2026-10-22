from notiontool import notion, main_wake


class FakeClient:
    info = {'space_id': 'space'}

    def model(self, selected='opus'):
        return 'default-opus'


def test_user_config_keeps_user_model(monkeypatch):
    monkeypatch.setattr(notion, 'inherit_main_config', lambda c, t: ('sonnet', 'user-model', 'max', 'kept'))
    assert notion.user_config(FakeClient(), 'thread') == ('user-model', 'max')


def test_user_config_fallback_is_never_auto(monkeypatch):
    def boom(c, t):
        raise RuntimeError('offline')
    monkeypatch.setattr(notion, 'inherit_main_config', boom)
    model, effort = notion.user_config(FakeClient(), 'thread')
    assert model == 'default-opus' and effort == notion.MAIN_DEFAULT_EFFORT


def test_wake_notice_sends_user_model(monkeypatch):
    monkeypatch.setattr(notion, 'inherit_main_config', lambda c, t: ('sonnet', 'user-model', 'medium', 'kept'))
    seen = {}

    class Adapter:
        def new_id(self, space):
            return 'step'

        def body(self, client, thread, step_id, text, model, computer=False, effort=None):
            seen.update(model=model, effort=effort)
            return {}

    class Transport:
        def stream(self, *a, **k):
            return iter(())

    client = FakeClient()
    client.transport = Transport()
    client.headers = lambda: {}
    wake = main_wake.MainWake.__new__(main_wake.MainWake)
    wake.adapter = Adapter()
    wake._send(client, 'thread', 'Cowork run finished')
    assert seen == {'model': 'user-model', 'effort': 'medium'}
