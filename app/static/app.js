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
