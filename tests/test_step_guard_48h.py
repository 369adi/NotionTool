from notiontool import step_guard
from tests.test_step_guard import A, M, NOW, W, Store

H = 3600
OLD = dict(step_soft_48h=8500, step_hard_48h=10000)


def fill(s, hours, per_hour, worker=W, start=NOW):
    for h in range(hours):
        step_guard.add_steps(s, worker, per_hour, now=start + h * H)
    return start + (hours - 1) * H


def test_48h_defaults_only_brake_coworkers():
    lim = step_guard.DEFAULTS
    assert lim['step_soft_48h'] == 25000 and lim['step_hard_48h'] == 30000


def test_48h_soft_never_pauses():
    s = Store(dict(OLD))
    last = fill(s, 22, 400)  # 8,800 steps, never more than 400 in 15 min
    now = last + 1000
    st = step_guard.state(s, now, A)
    assert st['steps_48h'] == 8800 and st['soft_48h'] and not st['hard_48h'] and st['level'] != 'pause'
    assert step_guard.start_wait(s, M, now=now) == (0, '')


def test_48h_hard_stops_only_new_coworker_turns_never_the_main():
    s = Store(dict(OLD))
    last = fill(s, 25, 400)  # 10,000 steps -> hard
    assert not [f for k, f in s.events if k == 'step_limit_stop' and '48 h' in str(f.get('detail'))]
    now = last + 1000
    st = step_guard.state(s, now, A)
    assert st['level'] != 'pause' and st['hard_48h']
    wait, reason = step_guard.start_wait(s, W, now=now)
    assert wait == 900 and '48 h' in reason
    assert step_guard.start_wait(s, M, now=now) == (0, '')  # the Main goes on


def test_48h_old_hours_fall_out_and_main_steps_count():
    s = Store()
    fill(s, 10, 400, worker=M)
    assert step_guard.state(s, NOW + 10 * H, A)['steps_48h'] == 4000
    assert step_guard.state(s, NOW + 60 * H, A)['steps_48h'] == 0


def test_48h_limits_from_settings():
    s = Store(dict(step_soft_48h=100, step_hard_48h=200))
    fill(s, 5, 50)
    assert step_guard.start_wait(s, W, now=NOW + 5 * H)[0] == 900


def test_seed_h48_merges_history():
    s = Store()
    step_guard.add_steps(s, W, 30, now=NOW)
    hour = str(int(NOW // H * H))
    total = step_guard.seed_h48(s, A, {hour: 20, str(int(NOW // H * H) - H): 500, '0': 999}, now=NOW + 10)
    assert total == 530  # this hour keeps 30 (larger), the hour before adds 500, too old is dropped
