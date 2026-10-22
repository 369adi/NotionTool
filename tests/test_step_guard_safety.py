"""Second safety net (AI steps / 60 min), nonstop rest, error brake, rate-limit snapshot."""
from notiontool import step_guard
from tests.test_step_guard import Store, W, M, NOW


def test_ai_steps_60m_stop_all_chats():
    s = Store()
    step_guard.add_steps(s, W, 300, now=NOW, inference=449)
    assert step_guard.state(s, NOW + 1)['level'] == 'ok'
    step_guard.add_steps(s, M, 10, now=NOW + 2, inference=1)  # Main AI steps count too
    st = step_guard.state(s, NOW + 3)
    assert st['level'] == 'pause' and 890 <= st['wait'] <= 900
    stops = [f for k, f in s.events if k == 'step_limit_stop']
    assert len(stops) == 1 and '450 AI steps' in stops[0]['detail']


def test_unknown_type_estimates_25_percent():
    s = Store()
    step_guard.add_steps(s, W, 40, now=NOW)
    item = s.get(step_guard.KIND, 'rolling')['items'][-1]
    assert item[5] == 10 and item[6] == 0


def test_count_new_reads_step_types():
    s = Store()
    steps = [dict(type='agent-inference'), dict(type='agent-tool-result'), dict(type='agent-inference'),
             dict(type='workflow-effect-error')]
    assert step_guard.count_new(s, W, steps, 0, now=NOW) == 4
    item = s.get(step_guard.KIND, 'rolling')['items'][-1]
    assert item[1] == 4 and item[5] == 2 and item[6] == 1


def test_coworker_rest_after_12h_nonstop_main_free():
    s = Store()
    for h in range(12):
        step_guard.add_steps(s, W, 150, now=NOW - (12 - h) * 3600 + 60)
    assert step_guard.state(s, NOW, 'acc1')['cw_streak_h'] == 12
    wait, reason = step_guard.start_wait(s, W, now=NOW)
    assert wait == 3600 and 'nonstop' in reason
    assert step_guard.start_wait(s, M, now=NOW) == (0, '')
    wait, reason = step_guard.start_wait(s, W, now=NOW + 100)
    assert 3400 < wait <= 3501 and 'rest' in reason
    assert step_guard.start_wait(s, W, now=NOW + 3700) == (0, '')  # rest over, the streak starts again
    assert any(k == 'coworker_rest' for k, _ in s.events)


def test_11h_is_no_streak():
    s = Store()
    for h in range(11):
        step_guard.add_steps(s, W, 150, now=NOW - (11 - h) * 3600 + 60)
    assert step_guard.start_wait(s, W, now=NOW) == (0, '')


def test_error_brake_coworkers_only():
    s = Store()
    step_guard.add_steps(s, W, 40, now=NOW, inference=10, errors=30)
    wait, reason = step_guard.start_wait(s, W, now=NOW + 60)
    assert wait == 300 and 'error' in reason
    assert step_guard.state(s, NOW + 60)['level'] == 'ok'  # Main is not stopped
    assert step_guard.start_wait(s, W, now=NOW + 3700) == (0, '')


def test_rate_limit_snapshot_saved():
    s = Store()
    step_guard.add_steps(s, W, 200, now=NOW - 1000, inference=50)
    step_guard.add_steps(s, M, 100, now=NOW - 30, inference=30, errors=2)
    assert step_guard.note_provider_limit(s, M, 'UserRateLimitResponse', now=NOW, key='k1')
    hits = s.get(step_guard.KIND, 'rolling')['hits']
    w = hits[-1]['windows']
    assert w['60']['steps'] == 100 and w['3600']['steps'] == 300 and w['3600']['ai_steps'] == 80
    assert w['3600']['coworker_steps'] == 200 and w['60']['errors'] == 2
    ev = [f for k, f in s.events if k == 'provider_rate_limited'][0]
    assert ev['snapshot']['windows']['900']['steps'] == 100
