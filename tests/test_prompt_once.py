import unittest

from notiontool import plain_protocol, prompt_once, main_switch
from notiontool.agent_guard import GUARD_RULE

GOAL = 'Build the new NotionTool dashboard with live progress and a clean sidebar for all coworkers.'
RUN = dict(goal=GOAL, context='Earlier: cookies moved, turn watcher fixed, ten coworkers set up and tested.' * 3,
           project_path='C:/proj', allocation=dict(mode='operator'))


def task(i, attempts=1):
    return dict(id='t%d' % i, role='executor', prompt='Do part %d only.' % i, acceptance='check %d' % i,
                attempts=attempts, deps=[], files=[])


def build(t):
    return plain_protocol.prompt(t, RUN, '', 'tok', False)


class PromptOnce(unittest.TestCase):
    def test_first_prompt_full_second_short(self):
        worker = dict(thread_id='A', context_base_seq=1)
        first, state = prompt_once.compact(worker, task(1), RUN, build(task(1)))
        self.assertIn(GOAL, first)
        self.assertIn(GUARD_RULE, first)
        worker['prompt_sent'] = state
        full = build(task(2))
        second, _ = prompt_once.compact(worker, task(2), RUN, full)
        self.assertNotIn(GOAL, second)
        self.assertNotIn(GUARD_RULE, second)
        self.assertNotIn('Earlier: cookies', second)
        self.assertIn('Do part 2 only.', second)
        self.assertIn('check 2', second)
        self.assertIn(prompt_once.NOTE, second)
        self.assertLess(len(second), len(full) / 2)

    def test_new_thread_or_rotation_gets_everything(self):
        worker = dict(thread_id='A', context_base_seq=1)
        _, state = prompt_once.compact(worker, task(1), RUN, build(task(1)))
        for change in (dict(thread_id='B'), dict(context_base_seq=2)):
            w = dict(worker, prompt_sent=state, **change)
            out, _ = prompt_once.compact(w, task(2), RUN, build(task(2)))
            self.assertIn(GOAL, out)
            self.assertIn(GUARD_RULE, out)

    def test_retry_and_disabled_send_everything(self):
        worker = dict(thread_id='A', context_base_seq=1)
        _, state = prompt_once.compact(worker, task(1), RUN, build(task(1)))
        worker['prompt_sent'] = state
        out, _ = prompt_once.compact(worker, task(2, attempts=2), RUN, build(task(2, attempts=2)))
        self.assertIn(GUARD_RULE, out)
        out, _ = prompt_once.compact(worker, task(3), RUN, build(task(3)), enabled=False)
        self.assertIn(GUARD_RULE, out)


class Store:
    def __init__(self):
        self.data, self.events = {}, []

    def get(self, kind, key, default=None):
        return self.data.get((kind, key), default)

    def put(self, kind, doc):
        self.data[(kind, doc['id'])] = doc

    def event(self, *a, **k):
        self.events.append(a)


class SwitchAwaitingMain(unittest.TestCase):
    def test_usable_brief_passes_awaiting_main_run(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), 'b.md')
        with open(path, 'wb') as f:
            f.write(b'# Brief\nall sections\n')
        store = Store()
        store.put('runs', dict(id='r1', state='awaiting_main'))
        rec = dict(id='s1', state='brief_requested', run_id='r1', brief_path=path)
        store.put(main_switch.KIND, rec)

        class Ops:
            supports_operator = False
        out = main_switch.advance(store, rec, Ops())
        self.assertEqual(out['state'], 'brief_ready')


if __name__ == '__main__':
    unittest.main()
