import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import STATES, TRANSITION_ROLES
from src.service import Service


def iso(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()


class ExceedanceTrackingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "exceedance item", "description": "tracking",
             "severity": "exceedance", "quantity": 5, "threshold": 10,
             "external_ref": "EX-1"}, "creator", "operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def reading(self, report, concentration, limit=10.0, days_ago=0.0):
        return self.service.add_reading(
            self.item["id"],
            {"sampled_at": iso(days_ago), "concentration": concentration,
             "limit_value": limit, "report_no": report},
            "sampler", "operator")

    def test_exceedance_opens_review_and_earliest_compliant_closes(self):
        over = self.reading("R-1", 12.0)
        self.assertTrue(over["created"])
        self.assertTrue(over["is_exceedance"])
        reviews = self.service.list_reviews(self.item["id"], "viewer")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["status"], "open")
        self.assertEqual(reviews[0]["reading_id"], over["id"])
        ok = self.reading("R-2", 8.0)
        self.assertFalse(ok["is_exceedance"])
        reviews = self.service.list_reviews(self.item["id"], "viewer")
        self.assertEqual(reviews[0]["status"], "closed")
        self.assertEqual(reviews[0]["closed_by_reading_id"], ok["id"])
        self.assertIsNotNone(reviews[0]["closed_at"])

    def test_compliant_reading_does_not_close_future_exceedance_review(self):
        ok_early = self.reading("R-1", 8.0, days_ago=5)
        over = self.reading("R-2", 12.0, days_ago=1)
        reviews = self.service.list_reviews(self.item["id"], "viewer")
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["status"], "open")
        self.assertEqual(reviews[0]["reading_id"], over["id"])
        self.assertFalse(ok_early["is_exceedance"])

    def test_duplicate_report_no_reuses_first_result(self):
        first = self.reading("R-1", 12.0)
        again = self.service.add_reading(
            self.item["id"],
            {"sampled_at": iso(), "concentration": 99.0, "limit_value": 10.0,
             "report_no": "R-1"}, "other", "operator")
        self.assertFalse(again["created"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["concentration"], 12.0)
        self.assertEqual(len(self.service.list_readings(self.item["id"], "viewer")), 1)
        self.assertEqual(len(self.service.list_reviews(self.item["id"], "viewer")), 1)

    def test_three_exceedances_in_30_days_escalate_and_never_downgrade(self):
        self.reading("R-1", 12.0, days_ago=2)
        self.reading("R-2", 13.0, days_ago=1)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["current_level"], "exceedance")
        self.reading("R-3", 14.0)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["current_level"], "major")
        self.assertEqual(item["exceedance_count_30d"], 3)
        self.reading("R-4", 1.0)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["current_level"], "major")
        self.assertEqual(item["open_reviews"], 0)

    def test_exceedances_outside_window_do_not_escalate(self):
        self.reading("R-1", 12.0, days_ago=40)
        self.reading("R-2", 12.0, days_ago=35)
        self.reading("R-3", 12.0, days_ago=1)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["current_level"], "exceedance")
        self.assertEqual(item["exceedance_count_30d"], 1)

    def test_open_review_blocks_close_and_keeps_state(self):
        self.reading("R-1", 12.0)
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], STATES[-1], current["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(
            self.service.get_item(self.item["id"], "viewer")["status"], STATES[-2])
        self.reading("R-2", 5.0)
        current = self.service.get_item(self.item["id"], "viewer")
        current = self.service.transition(current["id"], STATES[-1], current["version"],
                                          "reviewer", TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(current["status"], STATES[-1])

    def test_list_and_detail_expose_tracking_fields(self):
        self.reading("R-1", 12.0)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertEqual(item["exceedance_count_30d"], 1)
        self.assertEqual(item["current_level"], "exceedance")
        self.assertEqual(item["open_reviews"], 1)
        self.assertIsNotNone(item["nearest_deadline"])
        listed = self.service.list_items("viewer")[0]
        self.assertEqual(listed["exceedance_count_30d"], 1)
        self.assertEqual(listed["current_level"], "exceedance")
        self.assertIsNotNone(listed["nearest_deadline"])
        self.reading("R-2", 5.0)
        item = self.service.get_item(self.item["id"], "viewer")
        self.assertIsNone(item["nearest_deadline"])

    def test_boundary_and_validation(self):
        equal = self.reading("R-1", 10.0, limit=10.0)
        self.assertFalse(equal["is_exceedance"])
        self.assertEqual(self.service.list_reviews(self.item["id"], "viewer"), [])
        with self.assertRaises(ValidationError):
            self.service.add_reading(self.item["id"], {"sampled_at": "not-a-time",
                                     "concentration": 1, "limit_value": 1,
                                     "report_no": "R-2"}, "sampler", "operator")
        with self.assertRaises(ValidationError):
            self.service.add_reading(self.item["id"], {"sampled_at": iso(),
                                     "concentration": 1, "limit_value": 0,
                                     "report_no": "R-2"}, "sampler", "operator")
        with self.assertRaises(PermissionDenied):
            self.service.add_reading(self.item["id"], {"sampled_at": iso(),
                                     "concentration": 1, "limit_value": 1,
                                     "report_no": "R-2"}, "sampler", "viewer")

    def test_audit_chain_covers_tracking_events(self):
        self.reading("R-1", 12.0)
        self.reading("R-2", 13.0)
        self.reading("R-3", 14.0)
        self.reading("R-4", 5.0)
        actions = [e["action"] for e in self.service.audit("viewer", self.item["id"])]
        self.assertIn("reading", actions)
        self.assertIn("review_open", actions)
        self.assertIn("review_close", actions)
        self.assertIn("escalate", actions)
        self.assertTrue(self.repo.verify_audit_chain())


class LegacyUpgradeTest(unittest.TestCase):
    def test_existing_database_upgrades_automatically(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "legacy.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                severity TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 0,
                threshold REAL NOT NULL DEFAULT 1,
                status TEXT NOT NULL CHECK(status IN
                    ('reported','assessing','remediation','inspection','closed')),
                version INTEGER NOT NULL DEFAULT 1,
                external_ref TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                detail TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
                external_ref TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(item_id, external_ref)
            );
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                actor TEXT NOT NULL,
                detail TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                entry_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
        """)
        conn.close()
        repo = Repository(path)
        try:
            service = Service(repo)
            item = service.create_item(
                {"title": "legacy item", "description": "pre-upgrade db",
                 "severity": "watch", "quantity": 1, "threshold": 10,
                 "external_ref": "LEG-1"}, "creator", "operator")
            reading = service.add_reading(
                item["id"], {"sampled_at": iso(), "concentration": 20.0,
                             "limit_value": 10.0, "report_no": "LR-1"},
                "sampler", "operator")
            self.assertTrue(reading["is_exceedance"])
            detail = service.get_item(item["id"], "viewer")
            self.assertEqual(detail["exceedance_count_30d"], 1)
            self.assertEqual(detail["open_reviews"], 1)
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
