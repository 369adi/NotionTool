from notiontool import agent_guard, main_switch
from notiontool.agent_guard import GUARD_RULE, PLAN_RULE


def test_plan_rule_text():
    assert '/api/operator/plan' in PLAN_RULE
    assert 'delegate=true' in PLAN_RULE


def test_resume_instructions_carry_plan_rule():
    assert 'Delegate work to the Coworkers. ' + GUARD_RULE in main_switch.RESUME_INSTRUCTIONS
    assert PLAN_RULE in main_switch.RESUME_INSTRUCTIONS


def test_coworker_prompts_do_not_get_plan_rule():
    assert PLAN_RULE not in agent_guard.with_rule('Do the task.', coworker=True)
