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
            # Emby 新版本用 "Id"，旧版本只有 "ItemId"（同一个值的两个字段名，
            # 官方文档写的是"Id string — ItemId came first, so that is left for
            # compatability purposes"），这里两个都取一下，优先用 Id。
            lib_id = item.get("Id") or item.get("ItemId")
            result.append({"Id": lib_id, "Name": item.get("Name")})
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

        重要坑点（已在 Emby 官方论坛得到确认，见
        https://emby.media/community/topic/130313-seting-user-library-access-from-api/）：
        GET /Users/{id} 返回的 Policy 里会带一个 "BlockedMediaFolders": []
        字段，如果原样把它 POST 回去，Emby 服务端会出现"页面/接口显示已经
        保存成 EnabledFolders 指定的几个库，但实际用客户端登录该账号时却
        仍然能看到全部媒体库"的经典 bug —— 也就是设置在管理端看起来生效了，
        真正生效的库权限却没变。去掉这个字段（不要发送它，而不是发送空
        数组）就能让 EnabledFolders 真正生效。因此这里在合并完 patch 之后，
        统一把这个字段从要提交的 Policy 里剔除。
        """
        user = self.get_user(emby_user_id)
        policy = user.get("Policy", {}) or {}
        policy.update(policy_patch)
        policy.pop("BlockedMediaFolders", None)
        self._request("POST", f"/Users/{emby_user_id}/Policy", json=policy)
        return policy

    def set_libraries_and_permissions(
        self, emby_user_id, library_ids, enable_download,
        enable_download_transcoded, enable_upload,
    ):
        patch = {
            "EnableAllFolders": False,
            "EnabledFolders": list(library_ids),
            "EnableContentDownloading": bool(enable_download),
            # "允许下载需要转码的媒体"：对应 Emby 的媒体转换/同步转码权限。
            "EnableMediaConversion": bool(enable_download_transcoded),
            "EnableSyncTranscoding": bool(enable_download_transcoded),
            "AllowCameraUpload": bool(enable_upload),
            # 用户明确要求关闭这两项，不做成可配置项，统一关闭
            "EnableLiveTvAccess": False,
            "EnableLiveTvManagement": False,
        }
        return self.update_policy(emby_user_id, patch)

    def get_effective_policy_summary(self, emby_user_id, libraries):
        """
        从 Emby 实时拉取该用户当前真正生效的策略，用于在页面上做校验展示，
        排查"页面上勾选了但 Emby 里没生效"这类问题。
        """
        user = self.get_user(emby_user_id)
        policy = user.get("Policy", {}) or {}
        enabled_ids = policy.get("EnabledFolders") or []
        matched_names = [lib["Name"] for lib in libraries if lib["Id"] in enabled_ids]
        return {
            "enable_all_folders": policy.get("EnableAllFolders"),
            "enabled_folder_ids": enabled_ids,
            "matched_library_names": matched_names,
            "enable_download": policy.get("EnableContentDownloading"),
            "enable_media_conversion": policy.get("EnableMediaConversion"),
            "allow_camera_upload": policy.get("AllowCameraUpload"),
            "is_disabled": policy.get("IsDisabled"),
            "enable_live_tv": policy.get("EnableLiveTvAccess"),
        }

    def set_disabled(self, emby_user_id: str, disabled: bool):
        return self.update_policy(emby_user_id, {"IsDisabled": bool(disabled)})

    def delete_user(self, emby_user_id: str):
        self._request("DELETE", f"/Users/{emby_user_id}")
