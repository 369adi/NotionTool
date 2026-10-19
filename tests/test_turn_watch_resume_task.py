import unittest

from notiontool.turn_watch import TurnWatch


class Store:
    def __init__(self, tasks):
        self.tasks, self.events = tasks, []

    def all(self, kind):
        return list(self.tasks) if kind == 'tasks' else []

    def event(self, kind, *args, **fields):
        self.events.append(kind)


class NewAdapter:
    def __init__(self):
        self.calls = []

    def continue_turn(self, worker, text, step_id, task=None):
        self.calls.append((step_id, task))
        return True


class OldAdapter:
    def __init__(self):
        self.calls = []

    def continue_turn(self, worker, text, step_id):
        self.calls.append(step_id)
        return True


WORKER = {'id': 'acc:thread-1', 'thread_id': 'thread-1'}
TASK = {'id': 't1', 'worker_id': 'acc:thread-1', 'state': 'running', 'model': 'fast'}
OTHER = {'id': 't2', 'worker_id': 'acc:thread-2', 'state': 'running'}


class ResumeTaskTests(unittest.TestCase):
    def test_adapter_with_task_gets_the_active_task(self):
        adapter = NewAdapter()
        watch = TurnWatch(Store([OTHER, TASK]), adapter, None)
        watch.sending.add(WORKER['id'])
        watch.send(WORKER, 'step-1', 'key-1')
        self.assertEqual(adapter.calls, [('step-1', TASK)])
        self.assertNotIn(WORKER['id'], watch.sending)

    def test_old_adapter_still_works(self):
        adapter = OldAdapter()
        watch = TurnWatch(Store([TASK]), adapter, None)
        watch.send(WORKER, 'step-2', 'key-2')
        self.assertEqual(adapter.calls, ['step-2'])

    def test_no_active_task_keeps_the_plain_call(self):
        adapter = NewAdapter()
        watch = TurnWatch(Store([dict(TASK, state='done'), OTHER]), adapter, None)
        watch.send(WORKER, 'step-3', 'key-3')
        self.assertEqual(adapter.calls, [('step-3', None)])


if __name__ == '__main__':
    unittest.main()
