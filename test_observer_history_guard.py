"""Mount-rotation guard tests using temporary files; no live service or DB changes."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import observer_history_guard as guard


class HistoryGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.native = base / 'native'
        self.proc = base / 'proc'
        self.mounted = self.proc / '42/root' / self.native.relative_to('/')
        self.native.mkdir()
        self.mounted.mkdir(parents=True)
        for name in guard.FILES:
            (self.native / name).write_text('fixture')
            os.link(self.native / name, self.mounted / name)

    def rotate(self, name):
        (self.native / name).unlink()
        (self.native / name).write_text('new fixture')

    def check(self):
        return guard.refresh_if_stale(self.native, self.proc)

    @patch.object(guard, 'active_pid', return_value=42)
    @patch.object(guard.subprocess, 'run')
    def test_content_writes_do_not_restart_and_rotation_refreshes_only_observer(self, run, pid):
        (self.native / 'thread_history_1.sqlite-wal').write_text('new contents, same inode')
        self.assertFalse(self.check())
        run.assert_not_called()
        self.rotate('thread_history_1.sqlite-wal')
        self.assertTrue(self.check())
        run.assert_called_once_with(['systemctl', '--user', 'try-restart', 'clinx-observer.service'],
                                    check=True, timeout=15)

    @patch.object(guard, 'active_pid', return_value=42)
    @patch.object(guard.subprocess, 'run')
    def test_optional_companion_disappears_or_appears(self, run, pid):
        (self.native / 'state_5.sqlite-shm').unlink()
        self.assertTrue(self.check())
        (self.mounted / 'state_5.sqlite-shm').unlink()
        self.assertFalse(self.check())
        (self.native / 'state_5.sqlite-shm').write_text('recreated')
        self.assertTrue(self.check())

    @patch.object(guard.subprocess, 'run')
    def test_inactive_or_replaced_process_is_left_alone(self, run):
        with patch.object(guard, 'active_pid', return_value=None):
            self.assertFalse(self.check())
        self.rotate('state_5.sqlite-wal')
        with patch.object(guard, 'active_pid', side_effect=[42, 43]):
            self.assertFalse(self.check())
        run.assert_not_called()

    @patch.object(guard, 'active_pid', return_value=42)
    @patch.object(guard.subprocess, 'run')
    def test_missing_database_or_changing_source_waits(self, run, pid):
        before = guard.identities(self.native)
        self.rotate('state_5.sqlite-wal')
        with patch.object(guard, 'identities', side_effect=[before, {}, guard.identities(self.native)]):
            self.assertFalse(self.check())
        (self.native / 'state_5.sqlite').unlink()
        self.assertFalse(self.check())
        run.assert_not_called()

    @patch.object(guard, 'active_pid', return_value=42)
    @patch.object(guard.subprocess, 'run')
    def test_unknown_stat_error_never_restarts(self, run, pid):
        with patch.object(guard, 'identities', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.check()
        run.assert_not_called()
