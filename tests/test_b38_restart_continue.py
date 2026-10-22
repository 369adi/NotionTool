from notiontool.turn_watch import restart_continue, RESTART_WINDOW

START = 1_800_000_000


def tr(entities, status='idle'):
    return {'session': {'status': status},
            'patches': [{'op': 'put', 'entity': dict(e, sequence=i + 1)} for i, e in enumerate(entities)]}


def test_finished_turn_before_restart_continues():
    t = tr([{'id': 'u1', 'kind': 'user_message', 'content': 'mach weiter', 'created_at': START - 120},
            {'id': 'a1', 'kind': 'assistant_message', 'content': 'Build laeuft', 'created_at': START - 90}])
    hit = restart_continue(t, START)
    assert hit and hit['key'] == 'restart:a1'


def test_old_turn_or_running_or_after_start_is_left_alone():
    old = tr([{'id': 'a1', 'kind': 'assistant_message', 'created_at': START - RESTART_WINDOW - 5}])
    assert restart_continue(old, START) is None
    running = tr([{'id': 'a1', 'kind': 'assistant_message', 'created_at': START - 60}], status='running')
    assert restart_continue(running, START) is None
    later = tr([{'id': 'a1', 'kind': 'assistant_message', 'created_at': START + 60}])
    assert restart_continue(later, START) is None


def test_user_text_is_returned_for_stop_check():
    t = tr([{'id': 'u1', 'kind': 'user_message', 'content': 'stop.', 'created_at': START - 100},
            {'id': 'a1', 'kind': 'assistant_message', 'created_at': START - 90}])
    assert restart_continue(t, START)['user_text'].startswith('stop')
