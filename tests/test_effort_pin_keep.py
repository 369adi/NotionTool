"""Own chats keep the user's model and effort; Opus never falls back to its default 'medium'.

Live 26.10. (data/model_pin.log): base config without effort, last updated-config step {} -> the
request went out without any effort and Notion used the Opus default 'medium'.
"""
import json
import unittest

from notiontool.notion_shell import PIN_DEFAULT_EFFORT, pin_config_body

OPUS = 'albuquerque-quinn'
SONNET = 'achira-donut'


def body(thread, base_value, *configs):
    steps = [dict(type='config', value=dict(base_value))]
    steps += [dict(type='updated-config', value=dict(c)) for c in configs]
    steps.append(dict(type='user', value=[['hi']]))
    return json.dumps(dict(threadId=thread, transcript=steps))


def last_config(post):
    steps = json.loads(post)['transcript']
    return [s for s in steps if s['type'] == 'updated-config'][-1]['value']


class EffortPinKeepTest(unittest.TestCase):
    def test_live_case_no_effort_anywhere_goes_out_high_not_medium(self):
        pin = {}
        out = pin_config_body(body('a1', dict(model=OPUS), {'enableAgentDiffs': True}, {}, {}), pin)
        self.assertIsNotNone(out)
        self.assertEqual(last_config(out)['reasoningEffort'], PIN_DEFAULT_EFFORT)
        self.assertEqual(PIN_DEFAULT_EFFORT, 'high')
        self.assertNotIn('model', last_config(out))  # the chat's own model is not touched

    def test_user_pick_in_an_earlier_step_is_sent_again_unchanged(self):
        out = pin_config_body(body('a2', dict(model=OPUS), {'reasoningEffort': 'medium'},
                                   {'reasoningEffort': 'xhigh'}, {}), {})
        self.assertEqual(last_config(out)['reasoningEffort'], 'xhigh')

    def test_user_pick_medium_stays_medium(self):
        pin = {}
        self.assertIsNone(pin_config_body(body('a3', dict(model=OPUS), {'reasoningEffort': 'medium'}), pin))
        out = pin_config_body(body('a3', dict(model=OPUS), {}), pin)  # next turn: empty step
        self.assertEqual(last_config(out)['reasoningEffort'], 'medium')

    def test_explicit_last_step_passes_unchanged(self):
        self.assertIsNone(pin_config_body(body('a4', dict(model=OPUS), {'reasoningEffort': 'max'}), {}))

    def test_one_chat_never_changes_another(self):
        pin = {}
        pin_config_body(body('b1', dict(model=OPUS), {'reasoningEffort': 'low'}), pin)
        out = pin_config_body(body('b2', dict(model=OPUS), {}), pin)
        self.assertEqual(last_config(out)['reasoningEffort'], 'high')

    def test_chat_keeps_its_own_model(self):
        pin = {}
        self.assertIsNone(pin_config_body(body('c1', dict(model=SONNET), {'reasoningEffort': 'high'}), pin))
        out = pin_config_body(body('c1', dict(model='auto'), {}), pin)
        value = last_config(out)
        self.assertEqual((value['model'], value['reasoningEffort']), (SONNET, 'high'))

    def test_auto_effort_becomes_the_chats_last_pick(self):
        pin = {}
        pin_config_body(body('d1', dict(model=OPUS), {'reasoningEffort': 'xhigh'}), pin)
        out = pin_config_body(body('d1', dict(model=OPUS), {'reasoningEffort': 'auto'}), pin)
        self.assertEqual(last_config(out)['reasoningEffort'], 'xhigh')


if __name__ == '__main__':
    unittest.main()
