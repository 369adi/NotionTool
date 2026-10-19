import os
import tempfile
import unittest

from notiontool import handoff_files

REAL = '2ed5483a29a342ffaff38b01fb1ed451:3fda1b13-e54d-806b-a0a5-00a983a15c4a'
OTHER = '0123456789abcdef0123456789abcdef:3fda1b13-e54d-806b-a0a5-00a983a15c4a'


class FileKeyTests(unittest.TestCase):
    def test_real_worker_ids_get_a_safe_stable_unique_name(self):
        key = handoff_files.file_key(REAL)
        self.assertEqual(handoff_files.safe_worker_id(key), key)
        self.assertLessEqual(len(key), 64)
        self.assertNotIn(':', key)
        self.assertEqual(key, handoff_files.file_key(REAL))
        self.assertNotEqual(key, handoff_files.file_key(OTHER))

    def test_safe_ids_stay_unchanged_and_bad_input_is_refused(self):
        self.assertEqual(handoff_files.file_key('w-1'), 'w-1')
        self.assertNotIn('..', handoff_files.file_key('../../etc'))
        for bad in ('', None):
            with self.assertRaises(ValueError):
                handoff_files.file_key(bad)

    def test_handoff_round_trip_with_a_real_worker_id(self):
        with tempfile.TemporaryDirectory() as folder:
            key = handoff_files.file_key(REAL)
            record = handoff_files.write_handoff(folder, key, dict(handoff='Checkpoint text', old_thread='a',
                                                                   new_thread='b', run_id='r', task_id='t'), 1000.0)
            self.assertTrue(os.path.isfile(handoff_files.handoff_path(folder, key)))
            self.assertTrue(record['token'])
            handoff_files.mark_acked(folder, key, 1001.0)


if __name__ == '__main__':
    unittest.main()
