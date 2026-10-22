"""Effort-aware task time limits.

Live measurements (Sonnet 5.5, same prompt): effort low ~12 s, effort max 819 s and 1157 s.
A flat task_timeout (900 s) marked healthy max turns as overdue/blocked. The limit now scales
with the reasoning effort of the task.
"""

EFFORT_FACTOR = {'max': 3.0, 'xhigh': 2.0, 'high': 1.3}


def task_limit(settings, task=None):
    """Seconds a task may run before it counts as overdue."""
    base = float((settings or {}).get('task_timeout') or 900)
    effort = str((task or {}).get('effort') or '').strip().lower()
    return base * EFFORT_FACTOR.get(effort, 1.0)
