import tempfile, unittest

from notiontool import projects as P
from notiontool.store import Store


class ProjectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_create_gives_own_id_and_fields(self):
        a = P.create_project(self.store, ' Alpha ', now=10)
        b = P.create_project(self.store, 'Beta', now=11)
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual((a['name'], a['status'], a['created'], a['updated']), ('Alpha', 'active', 10, 10))
        self.assertEqual(self.store.get(P.PROJECTS, a['id']), a)
        self.assertEqual([p['id'] for p in P.list_projects(self.store)], [a['id'], b['id']])
        with self.assertRaises(P.ProjectError):
            P.create_project(self.store, '   ')

    def test_rename_keeps_id_and_created(self):
        a = P.create_project(self.store, 'Alpha', now=10)
        r = P.rename_project(self.store, a['id'], 'Gamma', now=20)
        self.assertEqual((r['id'], r['name'], r['created'], r['updated']), (a['id'], 'Gamma', 10, 20))
        self.assertEqual(self.store.get(P.PROJECTS, a['id'])['name'], 'Gamma')

    def test_open_makes_project_active(self):
        a = P.create_project(self.store, 'Alpha')
        self.assertEqual(P.current_project_id(self.store), '')
        self.assertEqual(P.open_project(self.store, a['id'])['id'], a['id'])
        self.assertEqual(P.current_project_id(self.store), a['id'])

    def test_unknown_ids_fail_clearly(self):
        for call in (lambda: P.rename_project(self.store, 'p-missing', 'X'),
                     lambda: P.open_project(self.store, 'p-missing'),
                     lambda: P.switch_project(self.store, 'p-missing'),
                     lambda: P.archive_project(self.store, 'p-missing'),
                     lambda: P.add_item(self.store, 'p-missing', 'goals', {'text': 'g'})):
            with self.assertRaises(P.ProjectError) as caught:
                call()
            self.assertIn('p-missing', str(caught.exception))
        self.assertEqual(self.store.all(P.STATE), [])

    def test_archived_project_cannot_become_active(self):
        a = P.create_project(self.store, 'Alpha')
        P.open_project(self.store, a['id'])
        done = P.archive_project(self.store, a['id'], now=30)
        self.assertEqual((done['status'], done['updated']), ('archived', 30))
        self.assertEqual(P.current_project_id(self.store), '')
        self.assertEqual(P.list_projects(self.store), [])
        self.assertEqual(len(P.list_projects(self.store, include_archived=True)), 1)
        for call in (lambda: P.open_project(self.store, a['id']), lambda: P.switch_project(self.store, a['id'])):
            with self.assertRaises(P.ProjectError) as caught:
                call()
            self.assertIn('archived', str(caught.exception))
        self.assertEqual(P.current_project_id(self.store), '')

    def test_switch_saves_intermediate_results_of_the_left_project(self):
        a = P.create_project(self.store, 'Alpha')
        b = P.create_project(self.store, 'Beta')
        P.open_project(self.store, a['id'])
        result = P.switch_project(self.store, b['id'], intermediate=[{'task_id': 't1', 'text': 'half done'}], now=40)
        self.assertEqual(result['id'], b['id'])
        self.assertEqual(P.current_project_id(self.store), b['id'])
        saved = P.project_items(self.store, a['id'], 'task_results')
        self.assertEqual(len(saved), 1)
        self.assertEqual((saved[0]['task_id'], saved[0]['text'], saved[0]['intermediate'], saved[0]['project_id'], saved[0]['created']),
                         ('t1', 'half done', True, a['id'], 40))
        self.assertEqual(P.project_items(self.store, b['id'], 'task_results'), [])

    def test_failed_switch_saves_nothing(self):
        a = P.create_project(self.store, 'Alpha')
        P.open_project(self.store, a['id'])
        with self.assertRaises(P.ProjectError):
            P.switch_project(self.store, 'p-missing', intermediate=[{'text': 'x'}])
        self.assertEqual(P.current_project_id(self.store), a['id'])
        self.assertEqual(P.project_items(self.store, a['id'], 'task_results'), [])

    def test_task_records_its_project(self):
        a = P.create_project(self.store, 'Alpha')
        task = {'id': 't7', 'title': 'Write docs'}
        tagged = P.assign_task_project(task, a['id'])
        self.assertEqual(tagged, {'id': 't7', 'title': 'Write docs', 'project_id': a['id']})
        self.assertNotIn('project_id', task)
        P.save_task(self.store, task, a['id'])
        self.assertEqual(self.store.get('tasks', 't7')['project_id'], a['id'])
        with self.assertRaises(P.ProjectError):
            P.assign_task_project(task, '')
        with self.assertRaises(P.ProjectError):
            P.save_task(self.store, task, 'p-missing')

    def test_per_project_stores_are_separate(self):
        a = P.create_project(self.store, 'Alpha')
        b = P.create_project(self.store, 'Beta')
        for kind in P.STORES:
            P.add_item(self.store, a['id'], kind, {'text': 'a-' + kind})
            P.add_item(self.store, b['id'], kind, {'text': 'b-' + kind})
        for kind in P.STORES:
            self.assertEqual([d['text'] for d in P.project_items(self.store, a['id'], kind)], ['a-' + kind])
            self.assertEqual([d['text'] for d in P.project_items(self.store, b['id'], kind)], ['b-' + kind])
        artifacts = self.store.all('artifacts')
        self.assertEqual(sorted(d['project_id'] for d in artifacts), sorted([a['id'], b['id']]))
        with self.assertRaises(P.ProjectError):
            P.add_item(self.store, a['id'], 'secrets', {'text': 'x'})

    def test_pure_helpers_do_not_change_their_input(self):
        base = P.new_project('Alpha', now=1, ident='p1')
        self.assertEqual(base, {'id': 'p1', 'name': 'Alpha', 'status': 'active', 'created': 1, 'updated': 1})
        self.assertEqual(P.renamed(base, 'Beta', now=2)['name'], 'Beta')
        self.assertEqual(P.archived(base, now=3)['status'], 'archived')
        self.assertEqual(base['name'], 'Alpha')
        self.assertEqual(base['status'], 'active')
        item = P.new_item('p1', 'goals', {'text': 'g'}, now=5, ident='g1')
        self.assertEqual(item, {'text': 'g', 'id': 'g1', 'project_id': 'p1', 'created': 5})


if __name__ == '__main__':
    unittest.main()
