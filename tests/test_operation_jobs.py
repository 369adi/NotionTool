import tempfile
import unittest

from notiontool.server import Application
from notiontool.store import Store


class InlinePool:
    def submit(self, fn):
        fn()

    def shutdown(self, wait=True):
        pass


class OperationJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)

    def make_app(self):
        app = Application(self.temp.name)
        app.admin.shutdown(wait=False)
        app.admin = InlinePool()
        self.addCleanup(app.store.db.close)
        return app

    @staticmethod
    def finished(app):
        return [(e['message'], e['state'], e['error']) for e in app.store.events() if e['kind'] == 'operation_finished']

    def test_periodic_job_keeps_one_record_and_logs_only_changes(self):
        app = self.make_app()
        title, key = app.BRIDGE_JOB_TITLE, app.BRIDGE_JOB_ID
        outcomes = iter([{'ok': 1}, {'ok': 1}, RuntimeError('tunnel down'), RuntimeError('tunnel down'), {'ok': 1}])

        def step():
            value = next(outcomes)
            if isinstance(value, Exception):
                raise value
            return value

        for _ in range(5):
            self.assertEqual(app.job(title, step, 'bridge', key=key), {'job_id': key})
        jobs = [j for j in app.store.all('jobs') if j['title'] == title]
        self.assertEqual([(j['id'], j['state']) for j in jobs], [(key, 'done')])
        self.assertEqual(self.finished(app), [(title, 'done', ''), (title, 'failed', 'tunnel down'), (title, 'done', '')])
        self.assertNotIn('bridge', app.account_operations)

    def test_other_jobs_keep_one_record_and_event_each(self):
        app = self.make_app()
        app.job('Sign in', lambda: {'ok': 1}, 'acct')
        app.job('Sign in', lambda: {'ok': 1}, 'acct')
        self.assertEqual(len([j for j in app.store.all('jobs') if j['title'] == 'Sign in']), 2)
        self.assertEqual(len(self.finished(app)), 2)

    def test_startup_drops_old_maintenance_records_and_done_noise(self):
        title = Application.BRIDGE_JOB_TITLE
        store = Store(self.temp.name)
        for n in range(3):
            store.put('jobs', dict(id=f'jold{n}', title=title, state='done', created=n, error='', result={}, account_id='bridge'))
        store.put('jobs', dict(id='jlogin', title='Sign in', state='running', created=5, error='', result={}, account_id='acct'))
        store.event('operation_finished', message=title, state='done', error='')
        store.event('operation_finished', message=title, state='failed', error='tunnel down')
        store.event('operation_finished', message='Sign in', state='done', error='')
        store.db.close()
        app = self.make_app()
        jobs = {j['id']: j for j in app.store.all('jobs')}
        self.assertEqual(set(jobs), {'jlogin'})
        self.assertEqual(jobs['jlogin']['state'], 'interrupted')
        self.assertEqual(self.finished(app), [(title, 'failed', 'tunnel down'), ('Sign in', 'done', '')])


if __name__ == '__main__':
    unittest.main()
