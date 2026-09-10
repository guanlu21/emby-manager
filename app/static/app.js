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

function copyCredentials(username, password) {
  const text = `用户名:${username}，密码:${password}`;
  navigator.clipboard.writeText(text).then(() => alert('用户名和密码已复制')).catch(() => {
    window.prompt('请复制以下内容', text);
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

function sortUsers(field) {
  const url = new URL(window.location.href);
  url.searchParams.set('sort', field);
  window.location.href = url.toString();
}
