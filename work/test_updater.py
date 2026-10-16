import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from notiontool import updater  # noqa: E402


class Store:
    def __init__(self, tasks=()):
        self.tasks, self.events, self.values = list(tasks), [], {}

    def all(self, kind):
        return self.tasks if kind == 'tasks' else []

    def settings(self):
        return self.values

    def event(self, kind, run_id='', **fields):
        self.events.append((kind, fields))


class App:
    def __init__(self, store, data_dir):
        self.store, self.data_dir = store, data_dir


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def open(self, request, timeout=0):
        self.seen.append((request.full_url, request.get_header('Authorization'), request.get_header('Accept')))
        return Response(self.routes[request.full_url])


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self._opener, self._token, self._build = updater._OPENER, updater.github_token, updater.build_number
        self.addCleanup(self.restore)

    def restore(self):
        updater._OPENER, updater.github_token, updater.build_number = self._opener, self._token, self._build

    def test_kinds_local_builds_never_update(self):
        self.assertEqual(updater.install_kind(self.dir, frozen=True, stamped=False), 'local')
        self.assertEqual(updater.install_kind(self.dir, frozen=True, stamped=True), 'release')
        self.assertEqual(updater.install_kind(self.dir, frozen=False), 'local')
        (Path(self.dir) / '.git').mkdir()
        self.assertEqual(updater.install_kind(self.dir, frozen=False), 'git')
        self.assertFalse(updater.Updater(App(Store(), self.dir), kind='local').enabled())

    def test_release_numbers_and_handoff_arguments(self):
        self.assertEqual((updater.release_number('build-17'), updater.release_number('v1.2'), updater.release_number('')), (17, 0, 0))
        args = ['main.py', '--data-dir', 'x', '--wait-for', '999999991']
        self.assertEqual(updater.handoff(args), ['main.py', '--data-dir', 'x'])

    def test_only_idle_without_running_tasks(self):
        self.assertTrue(updater.Updater(App(Store([{'state': 'done'}]), self.dir), kind='release').idle())
        self.assertFalse(updater.Updater(App(Store([{'state': 'running'}]), self.dir), kind='release').idle())

    def _release(self, payload, digest=None):
        api = updater.API + updater.REPO + '/releases/latest'
        release = {'tag_name': 'build-12', 'assets': [{'name': 'NotionTool.exe', 'url': 'https://api/exe'},
                                                      {'name': 'NotionTool.exe.sha256', 'url': 'https://api/sha'}]}
        updater._OPENER = Opener({api: json.dumps(release).encode(), 'https://api/exe': payload,
                                  'https://api/sha': (digest or hashlib.sha256(payload).hexdigest()).encode()})
        updater.github_token = lambda: 'secret-token'

    def test_newer_release_is_downloaded_and_verified(self):
        updater.build_number = lambda: 11
        self._release(b'new exe bytes')
        staged = updater.Updater(App(Store(), self.dir), kind='release').check_release()
        self.assertEqual(staged['label'], 'build-12')
        self.assertEqual(Path(staged['path']).read_bytes(), b'new exe bytes')
        self.assertTrue(all(auth == 'Bearer secret-token' for _, auth, _ in updater._OPENER.seen))
        self.assertEqual(updater._OPENER.seen[1][2], 'application/octet-stream')

    def test_same_build_and_bad_checksum_install_nothing(self):
        updater.build_number = lambda: 12
        self._release(b'x')
        self.assertIsNone(updater.Updater(App(Store(), self.dir), kind='release').check_release())
        updater.build_number = lambda: 3
        self._release(b'tampered', digest='0' * 64)
        with self.assertRaisesRegex(RuntimeError, 'Checksum'):
            updater.Updater(App(Store(), self.dir), kind='release').check_release()
        self.assertEqual(list(Path(self.dir).rglob('NotionTool-*')), [])

    def test_token_never_follows_a_redirect_to_storage(self):
        import urllib.request
        handler = updater._NoAuthRedirect()
        req = urllib.request.Request('https://api.github.com/x', headers={'Authorization': 'Bearer t'})
        new = handler.redirect_request(req, None, 302, 'Found', {}, 'https://objects.githubusercontent.com/y')
        self.assertIsNone(new.get_header('Authorization'))


if __name__ == '__main__':
    unittest.main()
