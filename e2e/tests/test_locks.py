import os

from e2ekit import locks
from tests.helpers import HomeCase


class LockTests(HomeCase):
    def test_project_lock_exclusive(self):
        a, b = locks.project_lock("demo"), locks.project_lock("demo")
        self.assertTrue(a.try_acquire("run"))
        self.assertFalse(b.try_acquire("run"))
        self.assertTrue(locks.project_lock("other").try_acquire("run"))
        a.release()
        self.assertTrue(b.try_acquire("run"))
        b.release()

    def test_sim_caps(self):
        first = locks.acquire_sims(5)  # asks too many: project cap is 2
        self.assertEqual([k for k, _ in first], [1, 2])
        second = locks.acquire_sims(2)  # machine cap is 3: one left
        self.assertEqual([k for k, _ in second], [3])
        self.assertEqual(locks.acquire_sims(2), [])
        self.assertEqual(locks.slot_usage()["sims_busy"], [1, 2, 3])
        first[0][1].release()
        third = locks.acquire_sims(2)
        self.assertEqual([k for k, _ in third], [1])
        for _, lk in second + third + first[1:]:
            lk.release()
        self.assertEqual(locks.slot_usage()["sims_busy"], [])

    def test_clean_removes_only_free_locks(self):
        held = locks.build_lock()
        held.try_acquire()
        free = locks.project_lock("demo")
        free.try_acquire()
        free.release()
        self.assertEqual(locks.clean_stale(), ["project-demo.lock"])
        self.assertTrue(os.path.exists(held.path))
        held.release()

    def test_lock_survives_clean_race(self):
        a = locks.build_lock()
        a.try_acquire()
        a.release()
        os.unlink(a.path)  # as if `e2e clean` removed it under us
        b = locks.build_lock()
        self.assertTrue(b.try_acquire())
        self.assertTrue(locks.is_busy(b.path))
        b.release()
