"""RAM limit and leftover cleanup for coworker commands (live 26.10.: a 17 GB python froze the PC)."""
import os
import subprocess
import sys
import time
import unittest

from notiontool import long_calls

FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def alive(pid):
    # bytes: tasklist prints German umlauts that the default codec can not decode
    out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'], capture_output=True,
                         creationflags=FLAGS).stdout or b''
    return (' %d ' % pid).encode() in out


@unittest.skipUnless(os.name == 'nt', 'Windows job objects')
class JobLimitTest(unittest.TestCase):
    def test_memory_hog_is_cut_off_at_the_limit(self):
        code = 'x=[]\nfor i in range(40):\n    x.append(bytearray(32*1024*1024))\nprint("never")'
        process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=FLAGS)
        job = long_calls.contain(process, limit_mb=300)
        self.assertIsNotNone(job)
        out, err, timed_out = long_calls.communicate(process, 60, FLAGS)
        peak = long_calls.release(job)
        self.assertFalse(timed_out)
        self.assertNotEqual(process.returncode, 0)
        self.assertNotIn(b'never', out)
        self.assertIn(b'MemoryError', err)
        self.assertIsNotNone(peak)
        self.assertLessEqual(peak, 300 + 64)  # Windows counts a little over the limit before refusing
        self.assertGreaterEqual(peak, 300 - 64)

    def test_leftover_grandchild_is_stopped(self):
        # The command starts a child that outlives it (like a test app or a stuck worker), then ends.
        code = ('import subprocess,sys\n'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],'
                'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=%d)\n'
                'print(p.pid,flush=True)' % FLAGS)
        process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=FLAGS)
        job = long_calls.contain(process)
        out, _, timed_out = long_calls.communicate(process, 30, FLAGS)
        self.assertFalse(timed_out)
        child = int(out.decode().split()[0])
        self.assertTrue(alive(child))
        long_calls.release(job)
        for _ in range(50):
            if not alive(child):
                break
            time.sleep(0.1)
        self.assertFalse(alive(child))

    def test_normal_command_is_unchanged(self):
        process = subprocess.Popen([sys.executable, '-c', 'print("ok")'], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=FLAGS)
        job = long_calls.contain(process)
        out, _, timed_out = long_calls.communicate(process, 30, FLAGS)
        peak = long_calls.release(job)
        self.assertEqual((out.strip(), timed_out, process.returncode), (b'ok', False, 0))
        self.assertLess(peak, 200)

    def test_release_without_job_is_harmless(self):
        self.assertIsNone(long_calls.release(None))

    def test_limit_from_env(self):
        old = os.environ.get('NT_JOB_MEM_MB')
        try:
            os.environ['NT_JOB_MEM_MB'] = '1000'
            self.assertEqual(long_calls.job_mem_mb(), 1000)
            os.environ['NT_JOB_MEM_MB'] = 'x'
            self.assertEqual(long_calls.job_mem_mb(), long_calls.JOB_MEM_MB)
            os.environ['NT_JOB_MEM_MB'] = '10'
            self.assertEqual(long_calls.job_mem_mb(), 256)
        finally:
            if old is None:
                os.environ.pop('NT_JOB_MEM_MB', None)
            else:
                os.environ['NT_JOB_MEM_MB'] = old


if __name__ == '__main__':
    unittest.main()
