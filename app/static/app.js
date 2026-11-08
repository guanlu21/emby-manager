function promptExtend(evt, formEl) {
  evt.preventDefault();
  const days = prompt("延长多少天？（输入负数可提前到期）", "30");
  if (days === null) return false;
  const n = parseInt(days, 10);
  if (isNaN(n)) {
    alert("请输入有效的数字");
    return false;
  }
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "days";
  input.value = n;
  formEl.appendChild(input);
  formEl.submit();
  return false;
}

// ---------------- 仪表盘：批量操作 ----------------

function toggleAllRows(checkbox) {
  document.querySelectorAll(".row-select").forEach(cb => cb.checked = checkbox.checked);
}

function getSelectedUserIds() {
  return Array.from(document.querySelectorAll(".row-select:checked")).map(cb => cb.value);
}

function _fillUserIds(formEl, ids) {
  formEl.querySelectorAll("input[name=user_ids]").forEach(el => el.remove());
  ids.forEach(id => {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "user_ids";
    input.value = id;
    formEl.appendChild(input);
  });
}

function batchExtend() {
  const ids = getSelectedUserIds();
  if (ids.length === 0) {
    alert("请先在列表里勾选要续期的用户");
    return;
  }
  const days = prompt(`将为选中的 ${ids.length} 个用户统一续期，延长多少天？（输入负数可提前到期）`, "30");
  if (days === null) return;
  const n = parseInt(days, 10);
  if (isNaN(n)) {
    alert("请输入有效的数字");
    return;
  }
  const form = document.getElementById("batch-extend-form");
  document.getElementById("batch-days-input").value = n;
  _fillUserIds(form, ids);
  form.submit();
}

function batchLibraries() {
  const ids = getSelectedUserIds();
  if (ids.length === 0) {
    alert("请先在列表里勾选要设置媒体库的用户");
    return;
  }
  window.location.href = "/users/batch-libraries?ids=" + ids.join(",");
}

function batchAction(action, confirmMsg) {
  const ids = getSelectedUserIds();
  if (ids.length === 0) {
    alert("请先在列表里勾选要操作的用户");
    return;
  }
  if (!confirm(`${confirmMsg}（共 ${ids.length} 个用户）`)) return;
  const form = document.getElementById(`batch-${action}-form`);
  _fillUserIds(form, ids);
  form.submit();
}

function randomText(length, alphabet) {
  let result = '';
  for (let i = 0; i < length; i++) result += alphabet[Math.floor(Math.random() * alphabet.length)];
  return result;
}

function fillRandomCredentials() {
  const username = document.querySelector('input[name=username]');
  const password = document.querySelector('input[name=password]');
  if (username) username.value = 'user_' + randomText(8, 'abcdefghjkmnpqrstuvwxyz23456789');
  if (password) password.value = randomText(12, 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789');
}

function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  // 很多 NAS/内网部署是走 http:// 而不是 https://，
  // 这种"非安全上下文"下 navigator.clipboard 根本不存在，
  // 直接调用 .writeText 会同步抛错、且不会走到 .catch，
  // 表现就是点了按钮"毫无反应"。这里改用兼容性更好的
  // document.execCommand('copy') 兜底。
  return new Promise((resolve, reject) => {
    try {
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(textarea);
      if (ok) resolve(); else reject(new Error('execCommand copy failed'));
    } catch (err) {
      reject(err);
    }
  });
}

function copyCredentials(username, password) {
  const text = `用户名：${username}，密码：${password}`;
  copyToClipboard(text).then(() => alert('已复制：' + text)).catch(() => {
    window.prompt('自动复制失败，请手动复制以下内容：', text);
  });
}

function resetAndCopyCredentials(userId) {
  if (!confirm('该用户的原密码未记录，将生成并设置一个新密码，是否继续？')) return;
  fetch(`/users/${userId}/reset-password`, {method: 'POST'})
    .then(response => response.json().then(data => ({ok: response.ok, data})))
    .then(result => {
      if (!result.ok) throw new Error(result.data.error || '操作失败');
      copyCredentials(result.data.username, result.data.password);
    })
    .catch(error => alert(error.message));
}

// ---------------- 操作日志：按浏览器所在时区显示时间 ----------------
// 服务端已按 APP_TZ（默认北京时间）渲染好兜底文本；这里再用浏览器的
// 本地时区覆盖一次，这样服务器在 UTC、人在其它时区时也显示正确时间。
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.log-time[data-ts]').forEach(el => {
    const ts = Number(el.dataset.ts);
    if (!Number.isFinite(ts)) return;
    const d = new Date(ts * 1000);
    const pad = n => String(n).padStart(2, '0');
    const text = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
                 `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    el.textContent = text;
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (tz) el.title = `浏览器时区：${tz}`;
  });
});
