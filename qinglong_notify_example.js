/**
 * Emby 用户到期提醒 —— 青龙面板任务脚本
 *
 * 使用方法：
 * 1. 打开青龙面板 -> 脚本管理，新建该文件（例如 emby_expire_notify.js），粘贴以下内容。
 * 2. 修改下面 EMBY_MGR_URL 中的地址：
 *      - 如果 emby-manager 和青龙在同一个 docker network，可以直接用容器名，例如：
 *        http://emby-manager:8000/api/qinglong/report
 *      - 否则用 NAS 的局域网 IP + 映射端口，例如：
 *        http://192.168.1.10:8000/api/qinglong/report
 * 3. 把 TOKEN 换成 emby-manager「设置」页面里显示的 Token。
 * 4. 在青龙「定时任务」里新建任务，选择这个脚本，建议每天跑 1-2 次，例如 "0 9,20 * * *"。
 * 5. 确保青龙自身已经在「通知设置」里配置好了任意一种推送方式
 *    (Bark / Server酱 / 钉钉 / 企业微信 / Telegram / PushPlus 等)，
 *    本脚本会调用青龙内置的 sendNotify，自动使用你配置好的推送渠道。
 */

const EMBY_MGR_URL = "http://emby-manager:8000/api/qinglong/report";
const TOKEN = "在这里粘贴设置页面显示的token";
const DAYS_BEFORE = 3; // 提前几天开始提醒

const https = require("https");
const http = require("http");

function fetchJson(url) {
  return new Promise((resolve, reject) => {
    const lib = url.startsWith("https") ? https : http;
    lib
      .get(url, (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          try {
            resolve(JSON.parse(data));
          } catch (e) {
            reject(e);
          }
        });
      })
      .on("error", reject);
  });
}

!(async () => {
  // 青龙脚本目录下自带 sendNotify.js，用于调用面板里配置的推送渠道
  const notify = require("./sendNotify");

  const url = `${EMBY_MGR_URL}?token=${TOKEN}&days=${DAYS_BEFORE}`;
  let report;
  try {
    report = await fetchJson(url);
  } catch (e) {
    console.log("请求 emby-manager 接口失败：", e.message);
    return;
  }

  if (report.error) {
    console.log("接口返回错误：", report.error, "（请检查 Token 是否正确）");
    return;
  }

  const lines = [];

  if (report.expired_today && report.expired_today.length) {
    lines.push("【今日到期，已自动停用】");
    report.expired_today.forEach((u) => {
      lines.push(`- ${u.username}${u.note ? "（" + u.note + "）" : ""}`);
    });
    lines.push("");
  }

  if (report.expiring_soon && report.expiring_soon.length) {
    lines.push(`【即将到期（${DAYS_BEFORE}天内）】`);
    report.expiring_soon.forEach((u) => {
      lines.push(`- ${u.username}：${u.expire_date}（剩 ${u.days_left} 天）`);
    });
  }

  if (!lines.length) {
    console.log("没有即将到期或今日到期的用户，无需通知。");
    return;
  }

  const title = "Emby 用户到期提醒";
  const content = lines.join("\n");
  console.log(content);
  await notify.sendNotify(title, content);
})();
