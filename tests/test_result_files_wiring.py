import json
import os
import tempfile
import unittest
from unittest import mock

from notiontool import operator_main, plain_protocol, result_files, run_context

LONG = 'Full detailed result. ' * 400


class WiringBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(result_files, 'ROOT', self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def task(self, **extra):
        task = dict(id='t1', role='executor', title='Part', prompt='Do the part.', acceptance='tests pass')
        task.update(extra)
        return task

    def run_(self):
        return dict(id='r1', goal='Overall goal', capacity=2)


class PromptTest(WiringBase):
    def test_coworker_prompt_has_result_file_hint(self):
        # Build 21: the hint only comes together with the project tool line (needs a project folder).
        run = dict(self.run_(), project_path=self.tmp.name)
        text = plain_protocol._prompt_text(self.task(), run, '', 'tok', True)
        path = result_files.path_for('r1', 't1')
        self.assertIn('nt_write_file', text)
        self.assertIn(path, text)
        self.assertIn('result_file', text)
        self.assertIn('Please reply with the result of your assigned part only', text)

    def test_added_instruction_is_short(self):
        hint = plain_protocol._result_file_hint(self.task(), self.run_(), True)
        path = result_files.path_for('r1', 't1')
        self.assertLessEqual(len(hint.replace(path, '')), 400)

    def test_no_hint_without_tools_or_ids(self):
        self.assertEqual(plain_protocol._result_file_hint(self.task(), self.run_(), False), '')
        self.assertEqual(plain_protocol._result_file_hint(self.task(id=None), self.run_(), True), '')
        text = plain_protocol._prompt_text(self.task(), self.run_(), '', 'tok', False)
        self.assertNotIn('nt_write_file', text)

    def test_other_roles_unchanged(self):
        text = plain_protocol._prompt_text(self.task(role='reviewer'), self.run_(), 'ctx', 'tok', True)
        self.assertNotIn('nt_write_file to', text)


class RunContextTest(WiringBase):
    def test_file_path_uses_summary_and_path(self):
        path = result_files.write('r1', 't1', LONG)
        reply = json.dumps(dict(status='done', summary='Short summary.', files=[], tests='OK', result_file=path))
        body = run_context.result_body(dict(task_id='t1', result=reply))
        self.assertEqual(body, 'Short summary.\nFull result: ' + path)
        block = run_context.task_prompt_budget([dict(task_id='t1', result=reply)], [], 10000)['text']
        self.assertIn('Full result: ' + path, block)
        self.assertNotIn(LONG, block)

    def test_result_file_field_without_summary(self):
        path = result_files.write('r1', 't2', LONG)
        body = run_context.result_body(dict(task_id='t2', result='Done the part. RESULT: DONE', result_file=path))
        self.assertTrue(body.endswith('Full result: ' + path))

    def test_fallback_when_file_missing(self):
        missing = os.path.join(self.tmp.name, 'gone.md')
        reply = json.dumps(dict(status='done', summary='S.', result_file=missing))
        self.assertEqual(run_context.result_body(dict(result=reply)), reply)
        self.assertEqual(run_context.result_body(dict(result='  plain text  ')), 'plain text')


class OperatorMainTest(WiringBase):
    def test_summary_with_file(self):
        path = result_files.write('r1', 't1', LONG)
        reply = json.dumps(dict(status='done', summary='First. Second.', result_file=path))
        self.assertEqual(operator_main.result_summary(reply), 'First. Second. Full result: ' + path)

    def test_fallback_without_file(self):
        missing = os.path.join(self.tmp.name, 'gone.md')
        reply = json.dumps(dict(status='done', summary='First. Second.', result_file=missing))
        self.assertEqual(operator_main.result_summary(reply), 'First.')
        self.assertEqual(operator_main.result_summary('{"summary": "Only. More."}'), 'Only.')
        self.assertEqual(operator_main.result_summary('Summary: Plain line. Rest.'), 'Plain line.')


if __name__ == '__main__':
    unittest.main()
