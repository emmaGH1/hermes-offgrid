import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import SESSION_RETENTION_SECONDS, StateStore  # noqa: E402

A = "+254700000001"
B = "+254700000002"


class StateTests(unittest.TestCase):
    def setUp(self):
        self.store = StateStore(sqlite3.connect(":memory:"))

    def tearDown(self):
        self.store.close()

    def test_last_answer_roundtrip_and_overwrite(self):
        self.assertIsNone(self.store.get_last_answer(A))
        self.store.set_last_answer(A, "one")
        self.store.set_last_answer(A, "two")
        self.store.set_last_answer(B, "other")
        self.assertEqual(self.store.get_last_answer(A), "two")
        self.assertEqual(self.store.get_last_answer(B), "other")

    def test_persists_across_connections(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.db")
            s1 = StateStore(sqlite3.connect(path))
            s1.set_last_answer(A, "kept 🙂")
            s1.admit_question("s1", A, 1000.0)
            s1.close()
            s2 = StateStore(sqlite3.connect(path))
            self.assertEqual(s2.get_last_answer(A), "kept 🙂")
            self.assertEqual(s2.admit_question("s1", A, 1001.0), "duplicate")
            s2.close()

    def test_close_is_repeatable(self):
        self.store.close()
        self.store.close()

    def test_accept_then_duplicate(self):
        self.assertEqual(self.store.admit_question("s1", A, 100.0), "accepted")
        self.assertEqual(self.store.admit_question("s1", A, 101.0), "duplicate")
        # same session id, different phone is distinct
        self.assertEqual(self.store.admit_question("s1", B, 101.0), "accepted")

    def test_duplicate_does_not_consume_rate(self):
        self.store.admit_question("s1", A, 100.0)
        for _ in range(10):
            self.assertEqual(self.store.admit_question("s1", A, 101.0), "duplicate")
        self.assertEqual(self.store.admit_question("s2", A, 102.0), "accepted")
        self.assertEqual(self.store.admit_question("s3", A, 103.0), "accepted")
        self.assertEqual(self.store.admit_question("s4", A, 104.0), "rate_limited")

    def test_rate_limit_per_phone_and_window(self):
        for i in range(3):
            self.assertEqual(self.store.admit_question(f"s{i}", A, 100.0 + i), "accepted")
        self.assertEqual(self.store.admit_question("s9", A, 110.0), "rate_limited")
        # other phone unaffected
        self.assertEqual(self.store.admit_question("s9", B, 110.0), "accepted")
        # window is (now-60, now]: t=100 still counts at 159.9, expires at 160.0
        self.assertEqual(self.store.admit_question("s10", A, 159.9), "rate_limited")
        self.assertEqual(self.store.admit_question("s10", A, 160.0), "accepted")
        # events now 101, 102, 160 -> full again
        self.assertEqual(self.store.admit_question("s11", A, 160.1), "rate_limited")

    def test_rate_limited_not_recorded_so_retry_can_pass_later(self):
        for i in range(3):
            self.store.admit_question(f"s{i}", A, 100.0)
        self.assertEqual(self.store.admit_question("late", A, 120.0), "rate_limited")
        self.assertEqual(self.store.admit_question("late", A, 161.0), "accepted")

    def test_custom_limit_and_validation(self):
        self.assertEqual(self.store.admit_question("a", A, 1.0, limit=1, window_seconds=10), "accepted")
        self.assertEqual(self.store.admit_question("b", A, 2.0, limit=1, window_seconds=10), "rate_limited")
        with self.assertRaises(ValueError):
            self.store.admit_question("c", A, 3.0, limit=0)

    def test_session_retention_expiry_and_cleanup(self):
        self.store.admit_question("old", A, 0.0)
        later = SESSION_RETENTION_SECONDS + 1.0
        self.assertEqual(self.store.admit_question("old", A, later), "accepted")
        conn = self.store._conn
        # stale rate events are purged by cleanup
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM ussd_rate_events WHERE ts <= ?", (later - 60,)).fetchone()[0], 0
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ussd_sessions").fetchone()[0], 1)

    def test_admission_rolls_back_on_error(self):
        conn = self.store._conn
        conn.execute("DROP TABLE ussd_rate_events")
        with self.assertRaises(sqlite3.Error):
            self.store.admit_question("s1", A, 1.0)
        self.assertFalse(conn.in_transaction)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ussd_sessions").fetchone()[0], 0)

    def test_works_with_autocommit_none_isolation(self):
        s = StateStore(sqlite3.connect(":memory:", isolation_level=None))
        self.assertEqual(s.admit_question("s", A, 1.0), "accepted")
        self.assertEqual(s.admit_question("s", A, 2.0), "duplicate")
        s.set_last_answer(A, "x")
        self.assertEqual(s.get_last_answer(A), "x")
        s.close()

    def test_no_credentials_columns(self):
        cols = {r[1] for t in ("ussd_last_answers", "ussd_sessions", "ussd_rate_events")
                for r in self.store._conn.execute(f"PRAGMA table_info({t})")}
        self.assertFalse({c for c in cols if "key" in c or "secret" in c or "token" in c})


if __name__ == "__main__":
    unittest.main()
