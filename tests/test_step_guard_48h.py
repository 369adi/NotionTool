from notiontool import step_guard
from tests.test_step_guard import A, M, NOW, W, Store

H = 3600


def fill(s, hours, per_hour, worker=W, start=NOW):
    for h in range(hours):
        step_guard.add_steps(s, worker, per_hour, now=start + h * H)
    return start + (hours - 1) * H


def test_48h_soft_blocks_new_coworkers_but_not_the_main():
    s = Store()
    last = fill(s, 22, 400)  # 8,800 steps, never more than 400 in 15 min
    now = last + 1000
    st = step_guard.state(s, now, A)
    assert st['steps_48h'] == 8800 and st['soft_48h'] and st['level'] != 'pause'
    wait, reason = step_guard.start_wait(s, W, now=now)
    assert wait == 600 and '48 h' in reason
    assert step_guard.start_wait(s, M, now=now) == (0, '')  # the Main is not a coworker start


def test_48h_hard_stops_all_chats_until_below_soft():
    s = Store()
    last = fill(s, 25, 400)  # 10,000 steps -> hard
    stops = [f for k, f in s.events if k == 'step_limit_stop']
    assert stops and stops[-1]['detail'].startswith('48 h budget')
    st = step_guard.state(s, last + 1000, A)
    assert st['level'] == 'pause' and '48 h' in st['reason']
    until = st['step_pause_until']
    # 10,000 - 4 x 400 = 8,400 < 8,500: the 4th hour must fall out
    assert until == NOW + 3 * H + 48 * H
    assert step_guard.state(s, until + 1, A)['level'] != 'pause'


def test_48h_old_hours_fall_out_and_main_steps_count():
    s = Store()
    fill(s, 10, 400, worker=M)
    assert step_guard.state(s, NOW + 10 * H, A)['steps_48h'] == 4000
    assert step_guard.state(s, NOW + 60 * H, A)['steps_48h'] == 0


def test_48h_limits_from_settings():
    s = Store(dict(step_soft_48h=100, step_hard_48h=200))
    fill(s, 3, 50)
    assert step_guard.start_wait(s, W, now=NOW + 3 * H)[0] == 600
