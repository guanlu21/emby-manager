import os
import tempfile
import unittest
from pathlib import Path

TEST_DIR = tempfile.mkdtemp(prefix="emby-manager-backup-test-")
os.environ["DB_PATH"] = os.path.join(TEST_DIR, "app.db")

from app import backup, db  # noqa: E402


class BackupTests(unittest.TestCase):
    def setUp(self):
        with db.DB_LOCK:
            for suffix in ("", "-wal", "-shm"):
                Path(db.DB_PATH + suffix).unlink(missing_ok=True)
        db.init_db()

    def test_restore_recovers_users_settings_and_logs(self):
        db.set_setting("setup_done", "1")
        db.set_setting("admin_password_hash", "backup-password-hash")
        db.set_setting("emby_url", "http://old-emby:8096")
        db.add_user("user-1", "alice", None, ["lib-1"], True, False, password="secret")
        db.add_log("alice", "create", "fixture")
        archive = backup.create_backup()

        db.set_setting("emby_url", "http://new-emby:8096")
        db.add_user("user-2", "bob", None, [], False, False)
        db.add_log("bob", "delete", "later data")

        result = backup.restore_backup(archive)

        self.assertTrue(result["ok"])
        self.assertEqual(db.get_setting("emby_url"), "http://old-emby:8096")
        self.assertEqual(db.get_setting("admin_password_hash"), "backup-password-hash")
        users = db.list_users(include_deleted=True)
        self.assertEqual([u["username"] for u in users], ["alice"])
        self.assertEqual(db.recent_logs(10)[0]["detail"], "fixture")

    def test_corrupt_archive_is_rejected_without_changing_database(self):
        db.init_db()
        db.set_setting("emby_url", "http://keep-this:8096")
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(b"not a zip archive")
        self.assertEqual(db.get_setting("emby_url"), "http://keep-this:8096")

    def test_archive_contains_expected_database(self):
        archive = backup.create_backup()
        manifest, database = backup.validate_backup(archive)
        self.assertEqual(manifest["format"], "emby-manager-backup")
        self.assertGreater(len(database), 0)


if __name__ == "__main__":
    unittest.main()
