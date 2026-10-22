"""refresh() registers guessed chat ids from the team_threads ledger (chats the app created) and skips all others.

The simulated Notion lists chats only through the space record, so every listed id is 'guessed' (like live
Notion since v23.13.20261017). Uses the real NotionAdapter code through FakeNotionAdapter(force=True).
"""
import tempfile
import unittest
import uuid
from pathlib import Path

from notiontool import fake_notion
from notiontool.store import Store


def norm(tid):
    return str(uuid.UUID(str(tid)))


class GuessedWelcomeTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        (tmp / 'app').mkdir()
        self.state = fake_notion.FakeState(path=tmp / 'fake_notion.json')
        self.store = Store(tmp / 'app')
        self.adapter = fake_notion.FakeNotionAdapter(self.store, state=self.state, force=True)
        self.adapter.refresh_on_login = False
        self.adapter.welcome_gap_s = 0
        self.account_id = self.adapter.browser_login()['account_id']
        settings = self.store.settings()
        settings['per_account'] = 1  # one Welcome Coworker keeps the test fast
        self.store.put('settings', settings)
        self.seeded = {norm(t['id']): t['title'] for t in self.state.threads_of('ready')}

    def tearDown(self):
        self.store.close()  # close the SQLite files before the folder is removed (Windows)
        self._tmp.cleanup()

    def worker_threads(self):
        return {norm(w['thread_id']): w for w in self.store.all('workers') if w['account_id'] == self.account_id}

    def set_ledger(self, ledger):
        account = self.store.get('accounts', self.account_id)
        account['team_threads'] = ledger
        self.store.put('accounts', account)

    def test_every_listed_id_is_guessed(self):
        listed = self.adapter.client(self.account_id).threads()
        self.assertEqual({norm(t['id']) for t in listed}, set(self.seeded))
        self.assertTrue(all(t.get('guessed') for t in listed))

    def test_unknown_guessed_ids_are_skipped(self):
        self.adapter.refresh(self.account_id)
        self.assertEqual(self.worker_threads(), {})

    def test_guessed_id_in_team_threads_gets_a_worker(self):
        titles = {title: tid for tid, title in self.seeded.items()}
        coworker, main = titles['Coworker 1'], titles['Main']
        self.set_ledger({'main': {'id': main, 'pending': False}, 'worker1': {'id': coworker, 'pending': False}})
        self.adapter.refresh(self.account_id)
        workers = self.worker_threads()
        self.assertEqual(set(workers), {main, coworker})
        self.assertEqual(workers[main]['purpose'], 'main')  # role from the ledger entry
        for tid, title in self.seeded.items():
            if tid not in (main, coworker):
                self.assertNotIn(tid, workers, title + ' is not in the ledger and must stay skipped')

    def test_team_setup_registers_its_created_welcome_chat(self):
        try:
            result = self.adapter.ensure_team(self.account_id)
        except ValueError as exc:
            self.fail('ensure_team failed: %s' % exc)
        self.assertEqual(result['team_size'], 1)
        ledger = self.store.get('accounts', self.account_id)['team_threads']
        created = norm(ledger['worker1']['id'])
        self.assertFalse(ledger['worker1']['pending'])
        self.assertNotIn(created, self.seeded)
        self.assertEqual(self.state.thread(ledger['worker1']['id'])['title'], fake_notion.WELCOME_TITLE)
        workers = self.worker_threads()
        self.assertIn(created, workers)
        self.assertEqual(workers[created]['join_state'], 'pending')
        self.assertTrue(workers[created]['pool_admitted'])
        # The user's own chats (unknown guessed ids) never become workers.
        for tid, title in self.seeded.items():
            self.assertNotIn(tid, workers, title + ' must stay skipped')


if __name__ == '__main__':
    unittest.main()
