"""回归测试：操作日志时间显示 + 页面版本号。

容器默认 UTC 时，旧代码把 Unix 时间戳直接按 UTC 渲染，北京时间会慢 8 小时。
"""
import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = tempfile.mkdtemp(prefix="emby-manager-time-test-")
os.environ["DB_PATH"] = os.path.join(TEST_DIR, "app.db")

from fastapi.testclient import TestClient  # noqa: E402
from app import VERSION, db, main  # noqa: E402

# 2026-11-08 05:01:35 UTC == 2026-11-08 13:01:35 北京时间
TS = int(datetime.datetime(2026, 11, 8, 5, 1, 35,
                           tzinfo=datetime.timezone.utc).timestamp())


class TimeFormatTests(unittest.TestCase):
    def test_timestamp_is_formatted_as_beijing_time(self):
        self.assertEqual(main._format_ts(TS), "2026-11-08 13:01:35")

    def test_app_tz_env_override(self):
        with mock.patch.dict(os.environ, {"APP_TZ": "UTC"}):
            self.assertEqual(main._format_ts(TS), "2026-11-08 05:01:35")

    def test_fallback_when_tz_name_is_invalid(self):
        with mock.patch.dict(os.environ, {"APP_TZ": "Not/A_Timezone"}):
            self.assertEqual(main._format_ts(TS), "2026-11-08 13:01:35")


class VersionTests(unittest.TestCase):
    def test_version_starts_at_v1_0(self):
        self.assertEqual(VERSION, "v1.0")

    def test_render_injects_version(self):
        request = mock.Mock()
        request.cookies = {}
        with mock.patch.object(main.templates, "TemplateResponse") as tr:
            main.render(request, "logs.html", logs=[])
        self.assertEqual(tr.call_args.args[1]["version"], "v1.0")


class PageTests(unittest.TestCase):
    def setUp(self):
        with db.DB_LOCK:
            for suffix in ("", "-wal", "-shm"):
                Path(db.DB_PATH + suffix).unlink(missing_ok=True)
        db.init_db()

    def test_login_page_shows_version(self):
        resp = TestClient(main.app).get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('name="app-version" content="v1.0"', resp.text)
        self.assertIn('<span class="version">v1.0</span>', resp.text)
        self.assertIn("/static/style.css?v=v1.0", resp.text)

    def test_logs_page_shows_localized_time_and_timestamp(self):
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO logs (ts, username, action, detail) VALUES (?,?,?,?)",
                (TS, "usr_test", "create", ""),
            )
        with mock.patch.object(main, "_guard", return_value=None):
            resp = TestClient(main.app).get("/logs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("2026-11-08 13:01:35", resp.text)
        self.assertIn('data-ts="%d"' % TS, resp.text)
        self.assertIn("v1.0", resp.text)


if __name__ == "__main__":
    unittest.main()
