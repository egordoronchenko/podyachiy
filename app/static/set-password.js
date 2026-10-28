initTheme();
/* Одноразовая ссылка — из письма или от администратора: /set-password#<строка>. Часть адреса
   после решётки браузер серверу не отправляет — строка уходит только телом запроса и не оседает
   в журнале запросов. */
const token = location.hash.slice(1);
const form = document.getElementById("form");
let minLength = 8, shown = false;

async function refuse(text) {
  // без почты новую ссылку прислать некому — её выпускает администратор
  let mail = false;
  try { mail = (await api("/api/me")).mail; } catch (e) { /* сервис недоступен — скажем про администратора */ }
  form.innerHTML = `<h2>Ссылка не действует</h2>
    <div class="note" role="alert">${esc(text)}</div>
    <p class="lead" style="margin-top:12px">Ссылка действует 48 часов и срабатывает один раз.
      Новая ссылка отменяет прежнюю${mail ? " — берите её из самого свежего письма" : ""}.
      ${mail ? "" : "Попросите новую ссылку у администратора сервиса."}</p>
    <div class="row">${mail ? `<a class="navlink" href="/login?forgot">Прислать новую ссылку</a>` : ""}
      <a class="navlink" href="/login">Ко входу</a></div>`;
}

function show(visible) {
  shown = visible;
  for (const id of ["pass", "again"]) document.getElementById(id).type = visible ? "text" : "password";
  const eye = document.getElementById("eye");
  eye.textContent = visible ? "Скрыть" : "Показать";
  eye.setAttribute("aria-pressed", String(visible));
}

const RULES = {
  pass: v => !v ? "Придумайте пароль" : v.length < minLength ? `Пароль короче ${minLength} знаков — сейчас ${v.length}` : "",
  again: v => !v ? "Повторите пароль" : v !== document.getElementById("pass").value ? "Пароли не совпадают" : "",
};
function check(id) {
  const problem = RULES[id](document.getElementById(id).value);
  fieldError(id, problem);
  return !problem;
}

async function start() {
  if (!token) return refuse("В адресе страницы нет одноразовой ссылки. Откройте ссылку целиком — так, как она пришла.");
  let who;
  try { who = await api("/api/set-password/check", { token }); }
  catch (err) { return refuse(err.status === 404 ? "Ссылка недействительна или её срок вышел." : err.message); }
  minLength = who.password_min;
  form.innerHTML = `
    <h2>Задать пароль</h2>
    <p class="lead">${esc(who.name)}, ваш логин — <b class="mono">${esc(who.login)}</b>. Придумайте пароль:
      с ним вы будете входить в сервис.</p>
    <input class="login-stub" type="text" autocomplete="username" value="${esc(who.login)}" tabindex="-1" aria-hidden="true" readonly>
    ${formField("pass", "Новый пароль",
      `<div class="passrow"><input type="password" id="pass" autocomplete="new-password" spellcheck="false"
          autocapitalize="none" aria-describedby="pass-hint pass-err" required autofocus>
        <button type="button" class="ghost" id="eye" aria-pressed="false">Показать</button></div>`,
      `Не короче ${minLength} знаков`, true)}
    ${formField("again", "Пароль ещё раз",
      `<input type="password" id="again" autocomplete="new-password" spellcheck="false" autocapitalize="none"
              aria-describedby="again-err" required>`, "", true)}
    ${revokeBox(who.keys || 0)}
    <div class="row"><button id="go">Сохранить пароль</button>
      <button type="button" class="ghost" id="gen">Придумать за меня</button></div>
    <div class="msg" id="msg" role="alert"></div>`;
  document.getElementById("pass").focus();
  document.getElementById("eye").addEventListener("click", () => show(!shown));
  document.getElementById("gen").addEventListener("click", () => {
    const made = newPassword();
    document.getElementById("pass").value = document.getElementById("again").value = made;
    show(true);
    fieldError("pass", ""); fieldError("again", "");
    say("msg", "", "Пароль придуман и показан в поле. Сохраните его у себя до того, как нажмёте «Сохранить пароль»: " +
                   "сервис его больше не покажет.");
  });
}

form.addEventListener("focusout", e => { if (RULES[e.target.id] && e.target.value) check(e.target.id); });
form.addEventListener("input", e => {
  if (e.target.getAttribute("aria-invalid") === "true") check(e.target.id);
  if (e.target.id === "pass" && document.getElementById("again").getAttribute("aria-invalid") === "true") check("again");
});

form.addEventListener("submit", async e => {
  e.preventDefault();
  const button = document.getElementById("go");
  if (!button || button.disabled) return;
  const bad = ["pass", "again"].filter(id => !check(id));
  if (bad.length) { document.getElementById(bad[0]).focus(); return; }
  button.disabled = true;
  button.textContent = "Сохраняю…";
  try {
    // ключи можно отозвать вместе с паролем — флажок есть, только если они у человека есть (revokeBox)
    const revoke = document.getElementById("revoke");
    const done = await api("/api/set-password", { token, password: document.getElementById("pass").value,
                                                  revoke_keys: !!(revoke && revoke.checked) });
    window.history.replaceState(null, "", "/set-password");     // использованная ссылка в адресе больше не нужна
    form.innerHTML = `<h2>Пароль сохранён</h2>
      <div class="done" role="status" tabindex="-1" id="done">${icon("ok", 18)}
        <span>Теперь войдите: логин <b class="mono">${esc(done.login)}</b> и пароль, который вы только что задали.${
          done.keys_revoked ? ` Ключи доступа отозваны: ${done.keys_revoked}.` : ""}</span></div>
      <div class="row"><a class="navlink" href="/login">Перейти ко входу</a></div>`;
    document.getElementById("done").focus();
    setTimeout(() => { location.href = "/login"; }, 4000);
  } catch (err) {
    button.disabled = false;
    button.textContent = "Сохранить пароль";
    say("msg", "err", err.message);
  }
});

start();
