import json
import unittest

from notiontool.operator_main import delegate, design_skip_reason
from tests import test_operator_main as base


def specified(*idents, ui=False):
    out = base.tasks(*idents)
    for t in out:
        t['files'] = ['notiontool/%s.py' % t['id']]
        if ui:
            t['title'] = 'Restyle the sidebar layout ' + t['id']
    return out


class DesignSkipTest(unittest.TestCase):
    def setUp(self):
        base.OperatorMainTest.setUp(self)

    def tearDown(self):
        base.OperatorMainTest.tearDown(self)

    def register(self):
        return base.OperatorMainTest.register(self)

    def run_delegate(self, tasks, goal='Fix the parser'):
        chat_id = self.register()['chat_id']
        result = delegate(self.engine, chat_id, goal, tasks, quality='high')
        run = self.store.get('runs', result['run_id'])
        titles = [t['title'] for t in self.engine.tasks(run['id'])]
        return run, titles

    def events(self, run_id, kind):
        rows = self.store.db.execute('SELECT doc FROM events WHERE run_id=? AND kind=?', (run_id, kind)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def set_setting(self, **values):
        self.store.settings = lambda: dict(values)

    def test_specified_non_ui_plan_skips_design(self):
        run, titles = self.run_delegate(specified('a', 'b'))
        self.assertFalse(any(t.startswith('Design:') for t in titles), titles)
        self.assertEqual(run['stage'], 'Building')
        events = self.events(run['id'], 'design_skipped')
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]['message'])
        for t in self.engine.tasks(run['id']):
            self.assertEqual(t['deps'], [])

    def test_ui_plan_keeps_design(self):
        run, titles = self.run_delegate(specified('a', ui=True))
        self.assertIn('Design: visual spec', titles)
        self.assertEqual(run['stage'], 'Designing')
        self.assertEqual(self.events(run['id'], 'design_skipped'), [])

    def test_setting_off_keeps_old_behavior(self):
        self.set_setting(skip_design_when_specified=False)
        run, titles = self.run_delegate(specified('a', 'b'))
        self.assertIn('Design: approach', titles)
        self.assertEqual(run['stage'], 'Designing')
        self.assertEqual(self.events(run['id'], 'design_skipped'), [])

    def test_missing_files_keeps_design(self):
        plan = specified('a', 'b')
        plan[1]['files'] = []
        run, titles = self.run_delegate(plan)
        self.assertIn('Design: approach', titles)
        self.assertEqual(run['stage'], 'Designing')

    def test_reason_helper(self):
        plan = [dict(files=['x.py'], acceptance='ok')]
        self.assertTrue(design_skip_reason({}, plan, False))
        self.assertEqual(design_skip_reason({}, plan, True), '')
        self.assertEqual(design_skip_reason({'skip_design_when_specified': False}, plan, False), '')
        self.assertEqual(design_skip_reason({}, [dict(files=['x.py'], acceptance=' ')], False), '')
        self.assertEqual(design_skip_reason({}, [dict(files=[], acceptance='ok')], False), '')


if __name__ == '__main__':
    unittest.main()
