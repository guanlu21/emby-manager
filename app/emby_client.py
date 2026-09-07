"""
Emby Server REST API 的最小封装。
参考: https://dev.emby.media/reference/RestAPI/
"""
import httpx


class EmbyError(Exception):
    pass


class EmbyClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0):
        if not base_url or not api_key:
            raise EmbyError("Emby 地址或 API Key 未配置，请先前往「设置」页面填写")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self):
        return {"X-Emby-Token": self.api_key, "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(self, method, path, **kwargs):
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(
                    method, self._url(path), headers=self._headers(), **kwargs
                )
        except httpx.RequestError as e:
            raise EmbyError(f"无法连接到 Emby 服务器: {e}")
        if resp.status_code >= 400:
            raise EmbyError(f"Emby 返回错误 {resp.status_code}: {resp.text[:300]}")
        return resp

    def test_connection(self):
        """验证地址与 API Key 是否有效，返回服务器信息"""
        resp = self._request("GET", "/System/Info")
        return resp.json()

    def list_libraries(self):
        """返回媒体库列表 [{Id, Name}]"""
        resp = self._request("GET", "/Library/VirtualFolders")
        data = resp.json()
        result = []
        for item in data:
            # VirtualFolders 返回的 ItemId 即 Policy.EnabledFolders 需要的 Id
            result.append({"Id": item.get("ItemId"), "Name": item.get("Name")})
        return result

    def list_emby_users(self):
        resp = self._request("GET", "/Users")
        return resp.json()

    def get_user(self, emby_user_id):
        resp = self._request("GET", f"/Users/{emby_user_id}")
        return resp.json()

    def create_user(self, username: str):
        resp = self._request("POST", "/Users/New", json={"Name": username})
        return resp.json()

    def set_password(self, emby_user_id: str, new_password: str):
        # 管理员用 API Key 重置用户密码，无需提供 CurrentPw
        self._request(
            "POST",
            f"/Users/{emby_user_id}/Password",
            json={"NewPw": new_password, "ResetPassword": False},
        )

    def update_policy(self, emby_user_id: str, policy_patch: dict):
        """
        Emby 要求 POST 完整 Policy 对象，因此先取当前 Policy 再合并覆盖。
        """
        user = self.get_user(emby_user_id)
        policy = user.get("Policy", {}) or {}
        policy.update(policy_patch)
        self._request("POST", f"/Users/{emby_user_id}/Policy", json=policy)
        return policy

    def set_libraries_and_permissions(self, emby_user_id, library_ids, enable_download, enable_upload):
        patch = {
            "EnableAllFolders": False,
            "EnabledFolders": library_ids,
            "EnableContentDownloading": bool(enable_download),
            "AllowCameraUpload": bool(enable_upload),
        }
        return self.update_policy(emby_user_id, patch)

    def set_disabled(self, emby_user_id: str, disabled: bool):
        return self.update_policy(emby_user_id, {"IsDisabled": bool(disabled)})

    def delete_user(self, emby_user_id: str):
        self._request("DELETE", f"/Users/{emby_user_id}")
