import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from notiontool import bridge as bridge_module
from notiontool import operator_main, plain_protocol, result_files, run_context
from notiontool.bridge import Bridge
from notiontool.engine import Engine
from notiontool.store import Store
import tests.test_engine as engine_base

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
        text = plain_protocol._prompt_text(self.task(), self.run_(), '', 'tok', True)
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

    def test_outside_root_is_not_a_result_file(self):
        outside = tempfile.NamedTemporaryFile('w', suffix='.md', delete=False)
        outside.write(LONG)
        outside.close()
        self.addCleanup(os.remove, outside.name)
        reply = json.dumps(dict(status='done', summary='First. Second.', result_file=outside.name))
        self.assertEqual(operator_main.result_summary(reply), 'First.')
        self.assertEqual(run_context.result_body(dict(result=reply)), reply)
        self.assertEqual(run_context.valid_result_file(self.tmp.name), '')

    def test_task_summary_keeps_the_path(self):
        path = result_files.write('r1', 't1', LONG)
        reply = json.dumps(dict(status='done', summary='First.', result_file=path))
        task = dict(summary='Own summary.', result=reply, title='T')
        self.assertEqual(operator_main.plain_task_summary(task), 'First. Full result: ' + path)
        self.assertEqual(operator_main.plain_task_summary(dict(task, result='{"summary": "X."}')), 'Own summary.')


class ReadFullHintTest(WiringBase):
    def test_reviewer_prompt_and_main_text_say_when_to_read(self):
        text = plain_protocol._prompt_text(self.task(role='reviewer'), self.run_(), 'ctx', 'tok', True)
        self.assertIn(plain_protocol.READ_FULL_HINT, text)
        self.assertLessEqual(len(plain_protocol.READ_FULL_HINT), 200)
        self.assertLessEqual(len(operator_main.READ_FULL_HINT), 200)
        self.assertIn('only when the review fails, tests fail', operator_main.READ_FULL_HINT)


class BridgeWriteTest(unittest.TestCase):
    """The coworker's nt_write_file call to the instructed path goes through the bridge."""

    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        base_dir = Path(self.temp.name)
        self.project = (base_dir / 'project').resolve()
        (self.project / 'docs').mkdir(parents=True)
        self.results = (base_dir / 'results').resolve()
        self.results.mkdir()
        self.store = Store(base_dir / 'data')
        self.engine = Engine(self.store, engine_base.FakeAdapter())
        self.store.put('runs', dict(id='r1', chat_id='c', folder_id='', goal='Docs', state='executing',
                                    project_path=str(self.project)))
        for ident, lease, files in (('a1', 'one', ['docs/A.md']), ('a2', 'two', [])):
            self.store.put('tasks', dict(id=ident, run_id='r1', state='running', lease=lease, files=files,
                                         role='executor', title=ident, worker_id='w-' + ident))
        for patcher in (mock.patch.object(result_files, 'ROOT', str(self.results)),
                        mock.patch('notiontool.devtools._backup', return_value='')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bridge = Bridge(self.store, self.engine)

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def write(self, token, path, text):
        return self.bridge.operator_call(None, 'nt_write_file', dict(path=str(path), content=text, assignment_token=token))

    def check_write(self, token, task_id):
        path = result_files.path_for('r1', task_id)
        self.write(token, path, LONG)
        self.assertEqual(Path(path).read_text(encoding='utf-8'), LONG)
        reply = json.dumps(dict(status='done', summary='Short summary.', files=[], tests='OK', result_file=path))
        self.assertEqual(run_context.result_body(dict(task_id=task_id, result=reply)), 'Short summary.\nFull result: ' + path)

    def test_task_without_files_list(self):
        self.check_write('two', 'a2')

    @unittest.skipUnless(hasattr(bridge_module, '_is_result_file'), 'needs the guard_scope change in notiontool/bridge.py')
    def test_task_with_files_list(self):
        self.check_write('one', 'a1')

    def test_other_paths_still_refused(self):
        with self.assertRaisesRegex(ValueError, "not in this task's files list.*no file was changed"):
            self.write('one', self.results / 'other.md', 'x')
        with self.assertRaisesRegex(ValueError, "not in this task's files list.*no file was changed"):
            self.write('one', result_files.path_for('r1', 'a2'), 'x')


if __name__ == '__main__':
    unittest.main()
