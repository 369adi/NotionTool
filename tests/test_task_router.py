import unittest

from notiontool import model_routing, task_router as tr


class TaskRouterTest(unittest.TestCase):
    def test_table_covers_every_class_and_risk(self):
        for cls in tr.TASK_CLASSES:
            for risk in tr.RISKS:
                rule = tr.RULES[(cls, risk)]
                self.assertIn(rule['model'], model_routing.MODELS)
                self.assertEqual(len(rule['fallback']), 2)
                self.assertNotIn(rule['model'], rule['fallback'])
        self.assertEqual(len(tr.RULES), len(tr.TASK_CLASSES) * len(tr.RISKS))

    def test_only_allowed_model_ids(self):
        allowed = set(model_routing.MODELS)
        self.assertEqual(set(tr.allowed_models()), allowed)
        for rule in tr.RULES.values():
            self.assertTrue({rule['model'], *rule['fallback']} <= allowed)
        for primary, chain in tr.FALLBACKS.items():
            self.assertTrue({primary, *chain} <= allowed)

    def test_reason_never_empty_and_uses_routing_reason(self):
        for (cls, risk), rule in tr.RULES.items():
            expected = model_routing.routing_reason('executor', rule['model'], cls, risk)
            self.assertTrue(rule['model_reason'].startswith(expected))
            self.assertIn(f'risk {risk}', rule['model_reason'])
        state = None
        for args in [('code', 'low', False), ('code', 'high', False), ('code', 'high', False),
                     ('code', 'high', True), ('x', 'y', False)]:
            out = tr.decide(args[0], args[1], state, failed=args[2])
            self.assertTrue(out['model_reason'].strip())
            state = out['state']

    def test_first_decision_uses_rule(self):
        out = tr.decide('code', 'high')
        self.assertEqual(out['model'], 'opus')
        self.assertEqual(out['fallback'], ['sonnet', 'haiku'])
        self.assertIn('Opus 5.5 is the default model', out['model_reason'])
        self.assertFalse(out['exhausted'])

    def test_stays_after_one_rule_change(self):
        first = tr.decide('code', 'low')
        self.assertEqual(first['model'], 'sonnet')
        second = tr.decide('code', 'high', first['state'])
        self.assertEqual(second['model'], 'sonnet')
        self.assertEqual(second['state']['pending'], 'opus')
        self.assertEqual(second['state']['streak'], 1)
        self.assertIn('kept by hysteresis', second['model_reason'])

    def test_switches_after_two_consecutive_changes(self):
        state = tr.decide('code', 'low')['state']
        state = tr.decide('code', 'high', state)['state']
        third = tr.decide('code', 'high', state)
        self.assertEqual(third['model'], 'opus')
        self.assertEqual(third['state']['streak'], 0)
        self.assertIsNone(third['state']['pending'])
        self.assertIn('2 consecutive decisions', third['model_reason'])

    def test_counter_resets_when_rule_goes_back(self):
        state = tr.decide('code', 'low')['state']
        state = tr.decide('code', 'high', state)['state']
        back = tr.decide('code', 'low', state)
        self.assertEqual(back['model'], 'sonnet')
        self.assertEqual(back['state']['streak'], 0)
        again = tr.decide('code', 'high', back['state'])
        self.assertEqual(again['model'], 'sonnet')
        self.assertEqual(again['state']['streak'], 1)

    def test_failure_switches_at_once(self):
        state = tr.decide('code', 'high')['state']
        out = tr.decide('code', 'high', state, failed=True)
        self.assertEqual(out['model'], 'sonnet')
        self.assertEqual(out['state']['failed'], ['opus'])
        self.assertEqual(out['fallback'], ['haiku'])
        self.assertIn('after failure', out['model_reason'])
        self.assertIn('fallback model', out['model_reason'])
        # The failed rule model is not picked again without a failure.
        later = tr.decide('code', 'high', out['state'])
        self.assertEqual(later['model'], 'sonnet')

    def test_fallback_chain_runs_out_cleanly(self):
        state = tr.decide('code', 'high')['state']
        models = []
        for _ in range(3):
            out = tr.decide('code', 'high', state, failed=True)
            models.append(out['model'])
            state = out['state']
        self.assertEqual(models, ['sonnet', 'haiku', None])
        self.assertTrue(out['exhausted'])
        self.assertEqual(out['fallback'], [])
        self.assertIn('exhausted', out['model_reason'])
        again = tr.decide('code', 'high', state, failed=True)
        self.assertTrue(again['exhausted'])
        self.assertIsNone(again['model'])

    def test_unknown_input_uses_documented_default(self):
        out = tr.decide('painting', 'extreme')
        default = tr.RULES[(tr.DEFAULT_TASK_CLASS, tr.DEFAULT_RISK)]
        self.assertEqual(out['model'], default['model'])
        self.assertIn("unknown task_class 'painting'", out['model_reason'])
        self.assertIn("unknown risk 'extreme'", out['model_reason'])
        empty = tr.rule_for('', None)
        self.assertEqual((empty['task_class'], empty['risk']), (tr.DEFAULT_TASK_CLASS, tr.DEFAULT_RISK))

    def test_input_state_is_not_mutated(self):
        state = tr.decide('code', 'low')['state']
        snapshot = {**state, 'failed': list(state['failed'])}
        tr.decide('code', 'high', state, failed=True)
        tr.decide('code', 'high', state)
        self.assertEqual(state, snapshot)


if __name__ == '__main__':
    unittest.main()
