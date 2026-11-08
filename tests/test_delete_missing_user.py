"""回归测试：Emby 侧已手动删除的用户，面板里必须还能删掉。

复现场景：DELETE /Users/{id} 返回 404 时，旧代码直接抛 EmbyError，
本地软删除没执行，面板里永远留一条僵尸记录。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TEST_DIR = tempfile.mkdtemp(prefix="emby-manager-delete-test-")
os.environ["DB_PATH"] = os.path.join(TEST_DIR, "app.db")

from app import main, db  # noqa: E402
from app.emby_client import EmbyClient, EmbyError  # noqa: E402


def not_found(text="找不到请求的用户。最近可能已从服务器中删除了。"):
    return EmbyError(f"Emby 返回错误 404: {text}", status_code=404)


class DeleteMissingUserTests(unittest.TestCase):
    def setUp(self):
        with db.DB_LOCK:
            for suffix in ("", "-wal", "-shm"):
                Path(db.DB_PATH + suffix).unlink(missing_ok=True)
        db.init_db()
        db.add_user("emby-id-ghost", "usr_vtvutqju", None, [], False, False)
        self.u = db.list_users()[0]

    def _client(self, delete_error=None, probe_error=None):
        client = mock.Mock()
        client.delete_user.side_effect = delete_error
        if probe_error is not None:
            client.get_user.side_effect = probe_error
        else:
            client.get_user.return_value = {"Id": "emby-id-ghost", "Name": "usr_vtvutqju"}
        return client

    def test_404_and_confirmed_missing_cleans_local_record(self):
        """DELETE 404 + GET 404 => 视为 Emby 侧已删除，继续清理面板记录。"""
        client = self._client(delete_error=not_found(), probe_error=not_found("Not Found"))
        with mock.patch.object(main, "_get_client_or_none", return_value=client):
            already_gone = main._do_delete(self.u)
        self.assertTrue(already_gone)
        self.assertEqual(db.list_users(), [])
        self.assertEqual(db.list_users(include_deleted=True)[0]["status"], "deleted")
        self.assertIn("仅清理面板记录", db.recent_logs(1)[0]["detail"])

    def test_non_404_error_still_blocks(self):
        """500 等真实错误不能被吞掉，本地记录保持原状。"""
        client = self._client(delete_error=EmbyError("Emby 返回错误 500: Server Error", 500))
        with mock.patch.object(main, "_get_client_or_none", return_value=client):
            with self.assertRaises(EmbyError):
                main._do_delete(self.u)
        self.assertEqual(db.list_users()[0]["status"], "active")

    def test_404_but_user_still_exists_is_not_swallowed(self):
        """DELETE 404 但 GET 仍能查到用户 => 说明是接口/代理问题，不能误判为已删除。"""
        client = self._client(delete_error=not_found())
        with mock.patch.object(main, "_get_client_or_none", return_value=client):
            with self.assertRaises(EmbyError):
                main._do_delete(self.u)
        self.assertEqual(db.list_users()[0]["status"], "active")

    def test_normal_delete_behaviour_unchanged(self):
        """正常删除仍调用 Emby 接口，且不标记为「已不存在」。"""
        client = self._client()
        with mock.patch.object(main, "_get_client_or_none", return_value=client):
            already_gone = main._do_delete(self.u)
        self.assertFalse(already_gone)
        client.delete_user.assert_called_once_with("emby-id-ghost")
        client.get_user.assert_not_called()
        self.assertEqual(db.list_users(), [])

    def test_no_emby_configured_still_cleans_local_record(self):
        client = None
        with mock.patch.object(main, "_get_client_or_none", return_value=client):
            self.assertFalse(main._do_delete(self.u))
        self.assertEqual(db.list_users(), [])


class EmbyErrorStatusTests(unittest.TestCase):
    def test_request_exposes_http_status_code(self):
        resp = mock.Mock()
        resp.status_code = 404
        resp.text = "找不到请求的用户"
        with mock.patch("app.emby_client.httpx.Client") as factory:
            factory.return_value.__enter__.return_value.request.return_value = resp
            client = EmbyClient("http://emby:8096", "test-key")
            with self.assertRaises(EmbyError) as ctx:
                client.delete_user("emby-id-ghost")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("404", str(ctx.exception))

    def test_non_http_error_has_no_status_code(self):
        self.assertIsNone(EmbyError("本地校验失败").status_code)


if __name__ == "__main__":
    unittest.main()
