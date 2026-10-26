import json, os, shutil, subprocess, tempfile, unittest
from datetime import datetime
from pathlib import Path

from notiontool import projects as P
from notiontool.store import Store

CONTRACT = Path(__file__).resolve().parent.parent / 'docs' / 'plan' / 'redesign' / 'api' / 'projects.json'


def git(repo, *args):
    subprocess.run(['git', '-c', 'user.name=Tester', '-c', 'user.email=t@example.com', '-c', 'commit.gpgsign=false',
                    '-C', str(repo), *args], check=True, capture_output=True)


class ProjectModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.temp.name, 'db'))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_create_has_all_fields_and_one_main_first(self):
        p = P.create_project(self.store, ' Alpha ', now=10, goal=' ship ', repo_path=' C:/r ', main_chat_id='c1', chat_ids=['c2', 'c1', 'c2'])
        self.assertEqual((p['name'], p['goal'], p['repo_path'], p['main_chat_id'], p['chat_ids']), ('Alpha', 'ship', 'C:/r', 'c1', ['c1', 'c2']))
        self.assertEqual((p['status'], p['created'], p['updated']), ('active', 10, 10))
        self.assertEqual(self.store.get(P.PROJECTS, p['id']), p)
        self.assertEqual(P.get_project(self.store, p['id']), p)
        bare = P.create_project(self.store, 'Beta')
        self.assertEqual((bare['goal'], bare['repo_path'], bare['main_chat_id'], bare['chat_ids']), ('', '', '', []))
        with self.assertRaises(P.ProjectError):
            P.create_project(self.store, '  ')

    def test_update_changes_only_given_fields(self):
        p = P.create_project(self.store, 'Alpha', now=10, main_chat_id='c1')
        u = P.update_project(self.store, p['id'], now=20, goal='G', repo_path='R')
        self.assertEqual((u['name'], u['goal'], u['repo_path'], u['main_chat_id'], u['chat_ids']), ('Alpha', 'G', 'R', 'c1', ['c1']))
        self.assertEqual((u['id'], u['created'], u['updated']), (p['id'], 10, 20))
        u = P.update_project(self.store, p['id'], name='Gamma', chat_ids=['c3'])
        self.assertEqual((u['name'], u['main_chat_id'], u['chat_ids']), ('Gamma', 'c1', ['c1', 'c3']))
        u = P.update_project(self.store, p['id'], main_chat_id='c3')
        self.assertEqual((u['main_chat_id'], u['chat_ids']), ('c3', ['c3', 'c1']))
        self.assertEqual(self.store.get(P.PROJECTS, p['id']), u)

    def test_update_rejects_bad_input_and_saves_nothing(self):
        p = P.create_project(self.store, 'Alpha', now=10)
        for call in (lambda: P.update_project(self.store, p['id'], status='archived'),
                     lambda: P.update_project(self.store, p['id'], name=' '),
                     lambda: P.update_project(self.store, 'p-missing', goal='x')):
            with self.assertRaises(P.ProjectError):
                call()
        self.assertEqual(self.store.get(P.PROJECTS, p['id']), p)

    def test_chat_belongs_to_one_active_project(self):
        a = P.create_project(self.store, 'Alpha', main_chat_id='c1', chat_ids=['c2'])
        b = P.create_project(self.store, 'Beta', main_chat_id='c9')
        with self.assertRaises(P.ProjectError) as caught:
            P.update_project(self.store, b['id'], chat_ids=['c2'])
        self.assertIn(a['id'], str(caught.exception))
        with self.assertRaises(P.ProjectError):
            P.create_project(self.store, 'Gamma', main_chat_id='c1')
        P.archive_project(self.store, a['id'])
        self.assertEqual(P.update_project(self.store, b['id'], chat_ids=['c2'])['chat_ids'], ['c9', 'c2'])

    def test_project_for_chat(self):
        a = P.create_project(self.store, 'Alpha', main_chat_id='c1', chat_ids=['c2'])
        self.assertEqual(P.project_for_chat(self.store, 'c2')['id'], a['id'])
        self.assertIsNone(P.project_for_chat(self.store, 'c3'))
        self.assertIsNone(P.project_for_chat(self.store, ''))
        P.archive_project(self.store, a['id'])
        self.assertIsNone(P.project_for_chat(self.store, 'c2'))
        self.assertEqual(P.project_for_chat(self.store, 'c2', include_archived=True)['id'], a['id'])

    def test_archive_is_a_flag_not_a_delete(self):
        a = P.create_project(self.store, 'Alpha', now=1)
        b = P.create_project(self.store, 'Beta', now=2)
        done = P.archive_project(self.store, a['id'], now=5)
        self.assertEqual((done['status'], done['updated'], done['name']), ('archived', 5, 'Alpha'))
        self.assertEqual([p['id'] for p in P.list_projects(self.store)], [b['id']])
        self.assertEqual(len(P.list_projects(self.store, include_archived=True)), 2)
        self.assertTrue(P.project_view(self.store, P.get_project(self.store, a['id']))['archived'])
        self.assertFalse(P.project_view(self.store, b)['archived'])

    def test_coworkers_come_from_project_tasks(self):
        a = P.create_project(self.store, 'Alpha', main_chat_id='c1')
        b = P.create_project(self.store, 'Beta')
        self.store.put('chats', dict(id='c1', main_worker='wm'))
        for tid, pid, worker in (('t1', a['id'], 'w1'), ('t2', a['id'], 'w2'), ('t3', a['id'], 'w1'),
                                 ('t4', a['id'], 'wm'), ('t5', b['id'], 'w9'), ('t6', a['id'], None)):
            self.store.put('tasks', dict(id=tid, project_id=pid, worker_id=worker))
        self.assertEqual(P.project_view(self.store, a)['coworker_ids'], ['w1', 'w2'])
        self.assertEqual(P.project_view(self.store, b)['coworker_ids'], ['w9'])

    def test_view_fills_fields_of_old_records(self):
        old = self.store.put(P.PROJECTS, P.new_project('Old', now=3, ident='p-old'))
        view = P.project_view(self.store, old)
        self.assertEqual((view['goal'], view['repo_path'], view['main_chat_id'], view['chat_ids'], view['coworker_ids']), ('', '', '', [], []))
        self.assertEqual(P.update_project(self.store, 'p-old', goal='g')['chat_ids'], [])

    def test_contract_matches_the_view(self):
        contract = json.loads(CONTRACT.read_text(encoding='utf-8'))
        p = P.create_project(self.store, 'Alpha', main_chat_id='c1')
        fields = set(P.project_view(self.store, p))
        self.assertEqual(set(contract['project_fields']), fields)
        self.assertEqual(set(contract['list_response']['projects'][0]), fields)
        detail = contract['detail_response']['project']
        self.assertEqual(set(detail), fields | {'commits'})
        self.assertEqual(set(detail['commits'][0]), set(contract['commit_fields']))
        self.assertEqual(set(P.project_detail(self.store, p['id'])), fields | {'commits'})


@unittest.skipUnless(shutil.which('git'), 'git is not installed')
class RecentCommitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, text):
        (self.repo / 'f.txt').write_text(text, encoding='utf-8')
        git(self.repo, 'add', 'f.txt')
        git(self.repo, 'commit', '-q', '-m', text)

    def test_newest_first_with_fields_and_limit(self):
        for text in ('one', 'two', 'three'):
            self.commit(text)
        commits = P.recent_commits(str(self.repo), n=2)
        self.assertEqual([c['subject'] for c in commits], ['three', 'two'])
        self.assertEqual(set(commits[0]), {'sha', 'subject', 'author', 'date'})
        self.assertEqual(commits[0]['author'], 'Tester')
        self.assertTrue(commits[0]['sha'].isalnum())
        datetime.fromisoformat(commits[0]['date'])
        self.assertEqual(len(P.recent_commits(str(self.repo))), 3)
        self.assertEqual(len(P.recent_commits(str(self.repo), n=0)), 1)

    def test_safe_without_a_repo(self):
        plain = Path(self.temp.name) / 'plain'
        plain.mkdir()
        self.assertEqual(P.recent_commits(str(plain)), [])
        self.assertEqual(P.recent_commits(str(self.repo)), [])  # git repo without a commit
        self.assertEqual(P.recent_commits(str(Path(self.temp.name) / 'missing')), [])
        self.assertEqual(P.recent_commits(''), [])
        self.assertEqual(P.recent_commits(None), [])


if __name__ == '__main__':
    unittest.main()
