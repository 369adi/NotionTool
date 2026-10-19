"""engine.py follow-ups: claim_absolute uses file_registry.path_key for one key per file."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from notiontool import file_registry
from notiontool.engine import Engine
from notiontool.store import Store


class NoRunAdapter:
    def run(self, worker, task, prompt, cancel, update):
        raise AssertionError('No dispatch expected')

    def recover(self, worker, task, cancel):
        raise AssertionError('No recovery expected')


class AbsoluteClaimTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / '.test-data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)
        self.base = Path(self.temp.name).resolve() / 'Sub'
        self.file = self.base / 'File.txt'
        for index in (1, 2):
            self.store.put('runs', dict(id='r%d' % index, chat_id='c%d' % index, state='executing',
                                        project_path=self.temp.name, capacity=1, worker_budget=1))
            self.store.put('tasks', dict(id='t%d' % index, run_id='r%d' % index, role='executor', state='running',
                                         lease='L%d' % index, worker_id='w%d' % index, files=[], deps=[], attempts=1))
        self.engine = Engine(self.store, NoRunAdapter())

    def tearDown(self):
        self.engine.close()
        time.sleep(.03)
        self.store.close()
        self.temp.cleanup()

    def spellings(self):
        text = str(self.file)
        result = [text.replace('\\', '/'), str(self.base / '.' / 'x' / '..' / 'File.txt')]
        if os.name == 'nt':
            result += [text.upper(), text.lower().replace('\\', '/')]
        return result

    def test_same_file_in_other_spellings_conflicts(self):
        self.engine.claim_absolute('t1', 'L1', str(self.file))
        self.assertEqual(self.store.get('tasks', 't1')['absolute_resources'], [file_registry.path_key(self.file)])
        for spelling in self.spellings():
            with self.assertRaisesRegex(ValueError, 'Another active task owns this PC path'):
                self.engine.claim_absolute('t2', 'L2', spelling)
        self.assertNotIn('absolute_resources', self.store.get('tasks', 't2'))

    def test_same_task_keeps_one_claim_for_all_spellings(self):
        for spelling in [str(self.file)] + self.spellings():
            self.engine.claim_absolute('t1', 'L1', spelling)
        self.assertEqual(self.store.get('tasks', 't1')['absolute_resources'], [file_registry.path_key(self.file)])

    def test_different_files_do_not_conflict(self):
        self.engine.claim_absolute('t1', 'L1', str(self.file))
        self.engine.claim_absolute('t2', 'L2', str(self.base / 'Other.txt'))
        self.engine.claim_absolute('t2', 'L2', str(self.base / 'File.txt.bak'))
        self.assertEqual(len(self.store.get('tasks', 't2')['absolute_resources']), 2)


if __name__ == '__main__':
    unittest.main()
