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
