import sqlite3, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class TrackingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        self.repo = Repository(self.db_path)
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "title": "track item", "description": "exceedance follow up",
            "severity": "normal", "quantity": 0, "threshold": 10,
            "external_ref": "TRK-1"}, "creator", "operator")
        self.iid = self.item["id"]
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _reading(self, concentration, report_no, sampled_at=None, limit=10.0):
        return self.service.register_reading(
            self.iid,
            {"sampled_at": iso(sampled_at or self.now), "concentration": concentration,
             "limit_value": limit, "report_no": report_no},
            "monitor", "operator")

    def test_exceedance_opens_review_and_compliant_closes(self):
        view = self.service.get_item(self.iid, "viewer")
        self.assertIsNone(view["next_deadline"])
        self.assertEqual(view["exceedance_count_30d"], 0)

        reading = self._reading(12.0, "R-1")
        self.assertTrue(reading["is_exceedance"])
        self.assertEqual(self.repo.open_review_count(self.iid), 1)
        view = self.service.get_item(self.iid, "viewer")
        self.assertEqual(view["exceedance_count_30d"], 1)
        self.assertIsNotNone(view["next_deadline"])

        # 未结复查还在时关闭返回409并保持原状态
        current = self.service.get_item(self.iid, "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "inspection")
        with self.assertRaises(ConflictError):
            self.service.transition(
                current["id"], STATES[-1], current["version"], "reviewer",
                TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(self.service.get_item(self.iid, "viewer")["status"], "inspection")

        # 之后最早的达标读数结清未结复查
        compliant = self._reading(8.0, "R-2", self.now + timedelta(hours=2))
        self.assertFalse(compliant["is_exceedance"])
        self.assertEqual(self.repo.open_review_count(self.iid), 0)
        self.assertIsNone(self.service.get_item(self.iid, "viewer")["next_deadline"])
        closed = self.service.transition(
            current["id"], STATES[-1], current["version"], "reviewer",
            TRANSITION_ROLES[STATES[-1]][0])
        self.assertEqual(closed["status"], "closed")

    def test_duplicate_report_keeps_first_result(self):
        first = self._reading(12.0, "DUP-1")
        again = self.service.register_reading(
            self.iid,
            {"sampled_at": iso(self.now + timedelta(days=1)), "concentration": 99.0,
             "limit_value": 10.0, "report_no": "DUP-1"},
            "monitor", "operator")
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["concentration"], 12.0)
        readings = self.service.list_readings(self.iid, "viewer")
        self.assertEqual(len(readings), 1)
        self.assertEqual(self.repo.open_review_count(self.iid), 1)

    def test_three_exceedances_in_30_days_escalate_and_stick(self):
        self._reading(11.0, "E-1", self.now - timedelta(days=20))
        self._reading(12.0, "E-2", self.now - timedelta(days=10))
        self._reading(13.0, "E-3", self.now - timedelta(days=1))
        view = self.service.get_item(self.iid, "viewer")
        self.assertEqual(view["current_level"], "major")
        self.assertEqual(view["severity"], "major")
        self.assertEqual(view["exceedance_count_30d"], 3)

        # 读数回落也不降级
        self._reading(5.0, "E-OK", self.now + timedelta(hours=1))
        view = self.service.get_item(self.iid, "viewer")
        self.assertEqual(view["current_level"], "major")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_spaced_exceedances_do_not_escalate(self):
        self._reading(11.0, "S-1", self.now - timedelta(days=70))
        self._reading(12.0, "S-2", self.now - timedelta(days=40))
        self._reading(13.0, "S-3", self.now - timedelta(days=2))
        view = self.service.get_item(self.iid, "viewer")
        self.assertNotEqual(view["current_level"], "major")
        self.assertEqual(view["exceedance_count_30d"], 1)

    def test_viewer_cannot_register_reading(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_reading(
                self.iid,
                {"sampled_at": iso(self.now), "concentration": 12.0,
                 "limit_value": 10.0, "report_no": "P-1"},
                "monitor", "viewer")

    def test_existing_database_upgrades_automatically(self):
        # 手工构造只含旧表的旧库
        old_path = str(Path(self.tmp.name) / "old.db")
        conn = sqlite3.connect(old_path)
        conn.executescript("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, description TEXT NOT NULL,
                severity TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 0,
                threshold REAL NOT NULL DEFAULT 1,
                status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                external_ref TEXT, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', external_ref TEXT,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
                actor TEXT NOT NULL, detail TEXT NOT NULL,
                previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO items(title,description,severity,quantity,threshold,status,"
            "version,external_ref,created_by,created_at,updated_at) "
            "VALUES('legacy','legacy item','normal',0,10,'reported',1,'OLD-1','op',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()

        upgraded = Repository(old_path)
        service = Service(upgraded)
        reading = service.register_reading(
            1,
            {"sampled_at": iso(self.now), "concentration": 15.0,
             "limit_value": 10.0, "report_no": "MIG-1"},
            "monitor", "operator")
        self.assertTrue(reading["is_exceedance"])
        view = service.get_item(1, "viewer")
        self.assertEqual(view["exceedance_count_30d"], 1)
        self.assertIsNotNone(view["next_deadline"])
        upgraded.close()


if __name__ == "__main__":
    unittest.main()
