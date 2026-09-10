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
        """
        返回媒体库列表 [{Id, Name}]，这里特意用 /Library/SelectableMediaFolders
        而不是 /Library/VirtualFolders。

        排查过程记录（重要，别改回 VirtualFolders）：
        Emby 的 /Users/{id}/Policy 使用的是媒体库 Guid，而不是页面树节点的
        Item Id。SelectableMediaFolders 同时返回 Id 和 Guid；这里统一把 Guid
        暴露为本程序内部的 Id，避免把错误的 Item Id 写入 EnabledFolders。
        而 /Library/SelectableMediaFolders 才是 Emby 官方"设置用户媒体库访问
        权限"专用的接口（官方文档字段里带有 IsUserAccessConfigurable），
        返回的 Guid 才是可以直接放进 EnabledFolders 里、能保证生效的值。

        另外这个接口返回的每一项都带 IsUserAccessConfigurable 字段：为
        false 表示这个"库"（常见于 合集/Collections 这类聚合视图）本身就
        不支持按用户限制访问，Emby 会让所有账号都能看到它，跟 EnabledFolders
        里勾不勾选没关系。这种条目直接从可勾选列表里过滤掉，避免管理员以为
        勾掉了它就能让某个用户看不到"合集"，实际上勾了也没用。
        """
        try:
            resp = self._request("GET", "/Library/SelectableMediaFolders")
            data = resp.json()
            return [
                # EnabledFolders 要求 Guid。只有极老版本未返回 Guid 时，
                # 才回退到 Id，避免新版本继续提交错误的 Item Id。
                {
                    "Id": item.get("Guid") or item.get("Id"),
                    "Guid": item.get("Guid"),
                    # 兼容 Guid 切换前已保存的媒体库排序配置。
                    "LegacyId": item.get("Id"),
                    "Name": item.get("Name"),
                }
                for item in data
                if item.get("IsUserAccessConfigurable", True)
            ]
        except EmbyError:
            # 极老版本 Emby 可能没有这个接口，退回到 VirtualFolders 作为兜底
            resp = self._request("GET", "/Library/VirtualFolders")
            data = resp.json()
            result = []
            for item in data:
                lib_id = item.get("Guid") or item.get("Id") or item.get("ItemId")
                result.append({
                    "Id": lib_id,
                    "Guid": item.get("Guid"),
                    "LegacyId": item.get("Id") or item.get("ItemId"),
                    "Name": item.get("Name"),
                })
            return result

    def list_emby_users(self):
        resp = self._request("GET", "/Users")
        return resp.json()

    def set_user_library_order(self, emby_user_id, library_ids):
        """把媒体库顺序写入 Emby 用户配置，供 Web/Vidhub 等客户端读取。"""
        user = self.get_user(emby_user_id)
        configuration = dict(user.get("Configuration", {}) or {})
        configuration["OrderedViews"] = list(library_ids)
        self._request(
            "POST",
            f"/Users/{emby_user_id}/Configuration",
            json=configuration,
        )
        fresh_configuration = self.get_user(emby_user_id).get("Configuration", {}) or {}
        actual_order = list(fresh_configuration.get("OrderedViews") or [])
        if actual_order != list(library_ids):
            raise EmbyError(
                f"Emby 用户 {emby_user_id} 的 OrderedViews 写入后不一致，"
                "网页端媒体库顺序未真正保存，请检查 Emby 版本或接口响应。"
            )

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

    def hide_user_from_login(self, emby_user_id: str):
        """
        隐藏用户，覆盖本地、远程及未识别设备登录界面。

        重要坑点（已用 Emby 官方 REST API 文档核实，见
        https://dev.emby.media/reference/RestAPI/UserService/postUsersByIdPolicy.html）：
        这三个开关虽然在后台网页版看起来像是"用户配置"，但实际上是
        UserPolicy 的字段（IsHidden / IsHiddenRemotely /
        IsHiddenFromUnusedDevices），必须 POST 到 /Users/{id}/Policy，
        而不是 /Users/{id}/Configuration。之前误当成 Configuration 字段
        提交，Emby 服务端会直接忽略这些不认识的字段——接口返回 200、
        不报错，但根本没有生效，导致用户一直没有真正被隐藏。
        """
        self.update_policy(emby_user_id, {
            "IsHidden": True,
            "IsHiddenRemotely": True,
            "IsHiddenFromUnusedDevices": True,
        })
        fresh_policy = self.get_user(emby_user_id).get("Policy", {}) or {}
        if not (
            fresh_policy.get("IsHidden")
            and fresh_policy.get("IsHiddenRemotely")
            and fresh_policy.get("IsHiddenFromUnusedDevices")
        ):
            raise EmbyError(
                f"Emby 用户 {emby_user_id} 的隐藏设置写入后未生效，"
                "请检查 Emby 服务端版本是否支持这几个字段。"
            )

    def update_policy(self, emby_user_id: str, policy_patch: dict):
        """
        Emby 要求 POST 完整 Policy 对象，因此先取当前 Policy 再合并覆盖。

        重要坑点（已在 Emby 官方论坛得到确认，见
        https://emby.media/community/topic/130313-seting-user-library-access-from-api/，
        并且用抓包比对过管理后台网页版真正保存时发出的请求体，实测确认）：
        如果原样把 GET /Users/{id} 返回的 "BlockedMediaFolders" 字段 POST
        回去，Emby 服务端会出现"页面/接口显示已经保存成 EnabledFolders 指定
        的几个库，但实际用客户端登录该账号、甚至打开管理后台该用户的访问
        页面时，媒体库勾选框却仍是空的"这个经典 bug——也就是保存看起来成
        功、GET 读回来也显示已生效，但真正生效的库权限其实没变。

        论坛实测确认必须完全不发送 "BlockedMediaFolders" 字段（而不是发送
        null 或空数组），这样服务端才会正确处理 EnabledFolders。所以这里
        统一从待提交的 Policy 中删除这个字段，而不是把它设为 None。
        """
        user = self.get_user(emby_user_id)
        policy = user.get("Policy", {}) or {}
        policy.update(policy_patch)
        # Emby 某些版本会因为请求体包含 BlockedMediaFolders（即使值为
        # null/空数组）而忽略 EnabledFolders，导致页面看似保存成功但用户
        # 实际没有任何媒体库权限。该字段必须从请求体中完全移除。
        policy.pop("BlockedMediaFolders", None)
        self._request("POST", f"/Users/{emby_user_id}/Policy", json=policy)
        return policy

    def set_libraries_and_permissions(
        self, emby_user_id, library_ids, enable_download,
        enable_download_transcoded, enable_upload,
    ):
        """
        写入媒体库/权限后，立刻重新从 Emby 拉一次该用户的 Policy 做校验，
        确认真的生效了，而不是"接口返回 200 就当作成功"。

        背景：管理端这里保存成功（HTTP 200，无异常）不等于 Emby 服务端真的
        应用了这次修改——历史上已经踩过 BlockedMediaFolders 字段导致"看起来
        保存成功、实际权限没变"的坑（见 update_policy 里的说明），不排除还
        有其它类似情况（比如某些 Emby 版本对未知/失效的库 Id 静默忽略、或者
        对 EnabledFolders 有额外的服务端校验）。与其让管理员在页面上看到
        "修改成功"之后还要手动点进「Emby 实时校验」去确认，这里直接在保存
        这一步就做同样的校验，不一致就当作失败抛出来，把问题原样暴露出去。
        """
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
        result = self.update_policy(emby_user_id, patch)

        # 保存后立即重新读取一次，校验真实生效的状态和刚才提交的是否一致
        fresh_policy = self.get_user(emby_user_id).get("Policy", {}) or {}
        actual_ids = set(fresh_policy.get("EnabledFolders") or [])
        expected_ids = set(library_ids)
        if fresh_policy.get("EnableAllFolders"):
            raise EmbyError(
                "已提交媒体库设置，但重新读取 Emby 发现 EnableAllFolders 仍为 True"
                "（不限制媒体库），保存没有真正生效，请重试或检查 Emby 服务端日志。"
            )
        if actual_ids != expected_ids:
            missing = expected_ids - actual_ids
            extra = actual_ids - expected_ids
            detail = []
            if missing:
                detail.append(f"缺少: {sorted(missing)}")
            if extra:
                detail.append(f"多出: {sorted(extra)}")
            raise EmbyError(
                "已提交媒体库设置，但重新读取 Emby 后发现实际生效的 EnabledFolders "
                f"和提交的不一致（{'; '.join(detail)}），保存没有真正生效，请重试。"
            )
        return result

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
