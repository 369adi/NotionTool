"""Steady coworkers instead of waves: 60 min admission keeps 10 % free, max 4 parallel."""
from notiontool import step_guard, long_calls
from tests.test_step_guard import Store, W, M, NOW


def test_default_max_parallel_is_4():
    assert step_guard.DEFAULTS['max_parallel_turns'] == 4


def test_60m_admission_waits_before_the_hard_stop():
    s = Store()
    # 1300 steps 30-50 min ago: outside 15 min, inside 60 min, below the 1500 hard stop
    step_guard.add_steps(s, W, 1300, now=NOW - 2400, inference=100)
    assert step_guard.state(s, NOW, 'acc1')['level'] != 'pause'
    need = step_guard.estimate(s, {})
    wait, reason = step_guard.start_wait(s, W, now=NOW, task={})
    if 1300 + need > 1350:
        assert wait == 120 and '60 min' in reason
    assert step_guard.start_wait(s, M, now=NOW, task={}) == (0, '')  # the Main is never admitted-blocked


def test_60m_admission_lets_small_load_through():
    s = Store()
    step_guard.add_steps(s, W, 200, now=NOW - 2400, inference=50)
    wait, reason = step_guard.start_wait(s, W, now=NOW, task={})
    assert '60 min' not in reason


def test_job_wait_is_one_long_wait():
    assert long_calls.wait_seconds(None) == 50.0
    assert long_calls.wait_seconds(999) == 50.0
