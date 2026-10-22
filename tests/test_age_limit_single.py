"""I32: the 23 h hard limit exists once; the coworker-count planner uses the shared constant."""
from notiontool import coworker_age_guard, parallel_plan


def test_planner_uses_the_shared_hard_limit():
    assert parallel_plan.DEFAULTS['max_age_h'] == float(coworker_age_guard.HARD_MAX_AGE_HOURS)


def test_hard_limit_is_23_hours():
    assert coworker_age_guard.HARD_MAX_AGE_HOURS == 23
