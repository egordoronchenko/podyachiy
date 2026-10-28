/* Общее для страницы статуса и страницы администрирования:
   значки, форматирование, тема, запросы к API и пароль. */

const STATUS_LABELS = { ok: "готово", no_audio: "нет звука",
                        no_summary: "нет протокола", error: "ошибка" };
const STATUS_ICONS = { ok: "ok", no_audio: "mute", no_summary: "doc", error: "alert" };
const THEME_KEY = "podyachiy-theme";

// Название и логотип сервиса — в одном месте: страницы берут их отсюда.
const BRAND = "Подьячий";           // в заголовке вкладки и надписью рядом с логотипом (шрифт надписи — в app.css)
const LOGO = "/static/logo.png";            // логотип целиком, на прозрачном фоне
const LOGO_SMALL = "/static/logo-small.png";

/* Оформление организации (корпоративный режим): свой логотип и короткое название вместо
   надписи. Приходит запросом /api/brand — он открыт и странице входа. Чтобы страница не
   мигала нашим логотипом перед своим, последнее оформление помнится в браузере и рисуется
   сразу, а запрос его уточняет. Название — текст организации: в разметку только через esc. */
const BRAND_KEY = "podyachiy-brand";
const pageTitle = document.title;
let brandNow = { name: "", logo: null };

function drawBrand() {
  const b = brandNow;
  document.querySelectorAll(".brand").forEach(el => {
    el.classList.toggle("own", !!b.name);
    el.setAttribute("aria-label", b.name || BRAND);      // надпись узорным шрифтом диктор читает обычным словом
    el.innerHTML = `<img src="${esc(b.logo ? b.logo.small : LOGO_SMALL)}" alt="" height="36"><span>${esc(b.name || BRAND)}</span>`;
  });
  const big = document.getElementById("loginlogo");
  if (big) big.src = b.logo ? b.logo.full : LOGO;
  const name = document.getElementById("loginname");       // на страницах входа надпись стоит под логотипом, в шапке её нет
  if (name) { name.textContent = b.name || BRAND; name.classList.toggle("own", !!b.name); }
  const tab = document.querySelector('link[rel="icon"]');
  if (tab) tab.href = b.logo ? b.logo.icon : "/static/favicon.png";
  document.title = b.name ? pageTitle.replace(BRAND, b.name) : pageTitle;
}

function setBrand(b) {
  brandNow = { name: (b && b.name) || "", logo: (b && b.logo) || null };
  try { localStorage.setItem(BRAND_KEY, JSON.stringify(brandNow)); } catch (e) { /* хранилище закрыто — обойдёмся */ }
  drawBrand();
}

function initBrand() {
  try {
    const kept = JSON.parse(localStorage.getItem(BRAND_KEY) || "null");
    if (kept && typeof kept === "object") brandNow = { name: String(kept.name || ""), logo: kept.logo || null };
  } catch (e) { /* в хранилище не то — нарисуем стандартное */ }
  drawBrand();
  fetch("/api/brand", { credentials: "same-origin" }).then(r => r.ok ? r.json() : null)
    .then(b => { if (b) setBrand(b); }).catch(() => {});
}

// Значки рисуем инлайновым SVG: эмодзи в роли значков выглядят разнобойно
// и по-разному отображаются в разных системах, а внешний файл со спрайтом
// подгружать неоткуда — страница работает без интернета.
const ICONS = {
  ok: '<path d="M20 6 9 17l-5-5"/>',
  mute: '<path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="m22 9-6 6"/><path d="m16 9 6 6"/>',
  doc: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="m9 15 3 3"/><path d="m12 15-3 3"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  copy: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  clip: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  coin: '<circle cx="12" cy="12" r="9"/><path d="M12 7v10"/><path d="M15 9.5a2.5 2.5 0 0 0-2.5-1.5h-1a2 2 0 0 0 0 4h1a2 2 0 0 1 0 4h-1A2.5 2.5 0 0 1 9 14.5"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  idle: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
  mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.9 4.9 1.4 1.4"/><path d="m17.7 17.7 1.4 1.4"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.3 17.7-1.4 1.4"/><path d="m19.1 4.9-1.4 1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 8.9 19a1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 5 8.9a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  back: '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>',
  close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  home: '<path d="m3 10 9-7 9 7v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22V12h6v10"/>',
  folder: '<path d="M4 20a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  play: '<path d="m7 4 13 8-13 8z"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9"/><path d="M16 3.1a4 4 0 0 1 0 7.8"/>',
  key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.8-9.8"/><path d="m16 7 3 3"/><path d="m18.5 4.5 2 2"/>',
  tasks: '<path d="m3 7 2 2 4-4"/><path d="m3 17 2 2 4-4"/><path d="M13 7h8"/><path d="M13 17h8"/>',
  mail: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
  undo: '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  list: '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/>',
};
const icon = (name, size = 15) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;

const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const mb = n => n >= 1048576 ? (n / 1048576).toFixed(1) + " МБ" : Math.round(n / 1024) + " КБ";
const hhmm = s => s ? String(s).slice(11, 19) : "—";
const dur = s => s == null ? "—" : (s < 90 ? Math.round(s) + " с" : (s / 60).toFixed(1) + " мин");
// Деньги — по валютам: {RUB: 12.1, USD: 0.03}. Валюты не пересчитываются и не складываются:
// запись, прошедшая через двух поставщиков, показывает обе суммы. Число вместо разбивки —
// строка журнала обработки из времён одной валюты, долларов.
const CURRENCY_SIGNS = { USD: "$", RUB: "₽" };
const sign = cur => CURRENCY_SIGNS[cur] || cur;
const amount = (v, cur) => `${(+v).toFixed(v > 0 && v < 0.1 ? 3 : 2).replace(".", ",")}\u00a0${sign(cur)}`;
const money = cost => {
  const parts = Object.entries(typeof cost === "number" ? { USD: cost } : cost || {}).filter(([, v]) => v);
  return parts.length ? parts.map(([cur, v]) => amount(v, cur)).join(" + ") : "—";
};

async function api(path, body) {
  const headers = { "Content-Type": "application/json" };
  const r = await fetch(path, { method: body === undefined ? "GET" : "POST", headers,
                                body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) {
    let detail = "HTTP " + r.status;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    // вход истёк или его не было: отправляем входить и возвращаем потом сюда же
    if (r.status === 401 && location.pathname !== "/login") location.href = loginUrl();
    const failure = new Error(detail);
    failure.status = r.status;
    failure.field = r.headers.get("X-Field") || "";     // к какому полю формы относится отказ
    throw failure;
  }
  return r.json();
}

/* Файл телом запроса (логотип, шаблон): сервер сам смотрит, что внутри. Ошибки — как у api(). */
async function putFile(path, file) {
  const r = await fetch(path, { method: "PUT", body: file, headers: { "Content-Type": "application/octet-stream" } });
  if (!r.ok) {
    let detail = "HTTP " + r.status;
    try { detail = (await r.json()).detail || detail; } catch (e) {}
    if (r.status === 401) location.href = loginUrl();
    throw new Error(detail);
  }
  return r.json();
}

/* ---------- поля форм ----------
   Подпись над полем, подсказка и ошибка — под ним. Ошибку объявляет и экранный диктор
   (role="alert"), а поле помечается aria-invalid: одного красного цвета мало. */
function formField(id, label, control, hint, required) {
  return `<label for="${id}">${label}${required ? `<span class="req" aria-hidden="true"> *</span>` : ""}</label>
    ${control}
    ${hint ? `<div class="fhint" id="${id}-hint">${hint}</div>` : ""}
    <div class="ferr" id="${id}-err" role="alert"></div>`;
}

function fieldError(id, text) {
  const input = document.getElementById(id), box = document.getElementById(id + "-err");
  if (!input || !box) return false;
  if (text) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid");
  box.innerHTML = text ? icon("alert", 14) + `<span>${esc(text)}</span>` : "";
  return true;
}

/* Пароль из латинских букв и цифр, без похожих знаков (0 и O, 1, l и I): его не перепутать
   при диктовке, и он без искажений проходит везде, куда его вписывают, — в форму входа,
   в настройки 1С, в адрес. 14 знаков из 55 — около 80 бит, перебором не взять. */
function newPassword() {
  const abc = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const limit = 256 - 256 % abc.length;       // байты выше предела отбрасываем, иначе знаки неравновероятны
  let out = "";
  while (out.length < 14) {
    for (const b of crypto.getRandomValues(new Uint8Array(32)))
      if (b < limit && out.length < 14) out += abc[b % abc.length];
  }
  return out;
}

/* Флажок «Отозвать мои ключи доступа» — при смене пароля (login.js) и на странице «задать пароль»
   (set-password.js), только тому, у кого ключи есть. По умолчанию выключен: на ключах работают
   интеграции человека (1С, ИИ-клиенты), и обрывать их без его слова нельзя; а если пароль мог узнать
   кто-то ещё, пояснение подсказывает отозвать и ключи — ключом входят без пароля. */
function revokeBox(count) {
  if (!count) return "";
  const word = count % 10 === 1 && count % 100 !== 11 ? "ключ"
    : [2, 3, 4].includes(count % 10) && ![12, 13, 14].includes(count % 100) ? "ключа" : "ключей";
  return `<label class="check"><input type="checkbox" id="revoke" aria-describedby="revoke-hint">
      <span><b>Отозвать мои ключи доступа</b> — у вас ${count} ${word}</span></label>
    <div class="fhint" id="revoke-hint">Программы и ИИ-клиенты, которые входят этими ключами, перестанут работать, пока
      вы не выпустите новые. Отзовите, если пароль или ключ мог узнать кто-то ещё: ключом входят без пароля.</div>`;
}

// ---------- кто вошёл ----------
let me = null;      // ответ /api/me: кто сейчас вошёл

// Страница входа с возвратом сюда же — вместе с меткой после «#»: по ней открывается окно
// «Ключи доступа» (/#keys из справки и из инструкции навыка), и терять её при входе нельзя.
const loginUrl = () => "/login?next=" + encodeURIComponent(location.pathname + location.search + location.hash);

async function initUser() {
  try { me = await api("/api/me"); } catch (e) { return; }
  // справка и описание API открыты и без входа: данных в них нет
  if (!me.login && !["/login", "/help", "/api"].includes(location.pathname)) {
    location.href = loginUrl();
    return;
  }
  const box = document.getElementById("account");
  if (!box || me.personal || !me.login) return;       // личный режим: входа нет, показывать некого
  const initials = me.name.trim().split(/\s+/).slice(0, 2).map(w => w[0]).join("").toUpperCase();
  box.innerHTML = `<span class="avatar" aria-hidden="true">${esc(initials)}</span>
    <span class="who"><b>${esc(me.name)}</b><span class="dim">${esc(me.login)}</span></span>`;
  const acts = document.getElementById("accountacts");
  // у admin из .env учётной записи в сервисе нет: его пароль в .env, почты у него нет; ключи — есть
  acts.innerHTML = `<button class="nav sm" id="keysbtn">${icon("key", 15)}<span>Ключи доступа</span></button>` +
    (me.login === "admin" ? "" :
      // у учётной записи домена пароля в сервисе нет: он меняется в домене
      `${me.mail ? `<button class="nav sm" id="notifybtn" role="switch"></button>` : ""}
       ${me.domain_account ? "" : `<a class="nav sm" href="/login?change">${icon("gear", 15)}<span>Сменить пароль</span></a>`}`) +
    `<button class="nav sm" id="logout">${icon("back", 15)}<span>Выйти</span></button>
     <button class="nav sm" id="logoutall" aria-expanded="false" aria-controls="logoutask">${icon("users", 15)}<span>Выйти везде</span></button>
     <div id="logoutask" role="alertdialog" aria-labelledby="logoutasktext" hidden></div>`;
  drawNotify();
  document.getElementById("logout").onclick = async () => {
    await api("/api/logout", {});
    location.href = "/login";
  };
  document.getElementById("logoutall").onclick = () => askLogoutAll(true);
  document.getElementById("keysbtn").onclick = openKeys;
  if (location.hash === "#keys") openKeys();      // сюда ведут справка и инструкция навыка
}

/* «Выйти везде»: входы на других браузерах и устройствах перестают действовать, этот остаётся.
   Сначала — подтверждение тут же, под кнопкой: по ошибке человек выкинул бы себя с телефона и
   ноутбука. Ключи доступа не трогаются — об этом сказано, чтобы не ждали, что программы отключатся. */
function askLogoutAll(open) {
  const box = document.getElementById("logoutask"), btn = document.getElementById("logoutall");
  btn.setAttribute("aria-expanded", String(open));
  box.hidden = !open;
  if (!open) { box.innerHTML = ""; btn.focus(); return; }
  box.innerHTML = `<div class="note dangerbox" id="logoutasktext">Выйти на всех остальных браузерах и устройствах?
      Этот браузер останется во входе. Ключи доступа продолжат работать — их отзывают в окне «Ключи доступа».</div>
    <div class="row"><button type="button" class="danger" id="logoutallgo">Выйти на остальных</button>
      <button type="button" class="ghost" id="logoutallno">Отмена</button></div>
    <div class="msg" id="logoutallmsg" aria-live="polite"></div>`;
  document.getElementById("logoutallno").onclick = () => askLogoutAll(false);
  box.onkeydown = e => { if (e.key === "Escape") { e.stopPropagation(); askLogoutAll(false); } };
  const go = document.getElementById("logoutallgo");
  go.onclick = async () => {
    go.disabled = true;
    go.textContent = "Выхожу…";
    try {
      await api("/api/logout/all", {});
      box.innerHTML = `<div class="msg ok" role="status" tabindex="-1" id="logoutalldone">Готово: на остальных браузерах и
        устройствах вход больше не действует. Здесь вы остались во входе.</div>`;
      document.getElementById("logoutalldone").focus();
      btn.setAttribute("aria-expanded", "false");
    } catch (err) {
      go.disabled = false;
      go.textContent = "Выйти на остальных";
      const msg = document.getElementById("logoutallmsg");
      msg.setAttribute("role", "alert");
      say("logoutallmsg", "err", "Не получилось: " + err.message);
    }
  };
  document.getElementById("logoutallno").focus();     // случайный Enter никого не выкинет
}

const ruDay = iso =>iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}` : "";

/* ---------- письма о готовности записи ----------
   Переключатель в меню учётной записи: «Уведомления на почту: когда моя запись готова».
   Включён, пока человек сам не отключит; без почты в учётной записи письму идти некуда —
   тогда он стоит выключенным и говорит, почему. Состояние написано словом, не только цветом. */
function drawNotify() {
  const btn = document.getElementById("notifybtn");
  if (!btn) return;
  const on = !!(me.email && me.notify_ready);
  btn.setAttribute("aria-checked", on);
  btn.setAttribute("aria-label", "Уведомления на почту: когда моя запись готова");
  btn.title = me.email
    ? `Уведомления на почту: когда моя запись готова — письмо со ссылкой и кратким итогом на ${me.email}`
    : "Уведомления на почту: в вашей учётной записи не указана почта — попросите администратора её вписать";
  btn.innerHTML = `${icon("mail", 15)}<span>Письма о готовности</span>
    <span class="sw${on ? " on" : ""}" aria-hidden="true">${me.email ? (on ? "вкл" : "выкл") : "нет почты"}</span>`;
  btn.onclick = async () => {
    if (!me.email) { alert(btn.title); return; }
    btn.disabled = true;
    try { me.notify_ready = (await api("/api/notify", { ready: !me.notify_ready })).notify_ready; }
    catch (e) { alert("Не сохранилось: " + e.message); }
    btn.disabled = false;
    drawNotify();
    btn.focus();
  };
}

/* ---------- протокол на почту ----------
   Окно открывается кнопкой «Отправить» в шапке записи — у хозяина папки. Получатели: галочки
   у тех, кому папка открыта и у кого в сервисе указана почта, плюс любые адреса вручную.
   Каждому получателю уходит своё письмо. Что и кому ушло, остаётся на экране после отправки —
   вместе с адресами, которым письмо не ушло, и причиной. */
let sendState = null;       // { file, people, max, noteMax, domains, busy, done }

async function openSend() {
  const file = viewFile;
  let info;
  try { info = await api(`/api/record/recipients?file=${encodeURIComponent(file)}`); }
  catch (e) { alert("Окно отправки не открылось: " + e.message); return; }
  if (viewFile !== file) return;                // пока ждали ответа, запись закрыли
  let box = document.getElementById("sendbox");
  if (!box) {
    box = document.createElement("div");
    box.id = "sendbox";
    document.body.appendChild(box);
    let downOnVeil = false;
    box.addEventListener("mousedown", e => { downOnVeil = e.target.id === "sveil"; });
    box.addEventListener("click", e => {
      if (e.target.closest("#sclose, #scancel") || (e.target.id === "sveil" && downOnVeil)) closeSend();
    });
    box.addEventListener("submit", runSend);
    // Escape закрывает это окно, а не запись под ним; Tab ходит по кругу внутри окна
    box.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.stopPropagation(); closeSend(); return; }
      if (e.key !== "Tab") return;
      const stops = [...box.querySelectorAll("button:not(:disabled), input, textarea")];
      const first = stops[0], last = stops[stops.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
  }
  sendState = { file, people: info.people, max: info.max, noteMax: info.note_max, domains: info.domains,
                busy: false, done: null };
  drawSend();
  (box.querySelector("input[name=sperson]") || document.getElementById("sto")).focus();
}

function drawSend() {
  const { file, people, max, noteMax, domains, done } = sendState;
  const name = file.split("/").pop();
  const head = `<div class="veil" id="sveil"><form class="modal" id="sform" role="dialog" aria-modal="true"
      aria-labelledby="stitle" novalidate>
    <header><h2 id="stitle">Отправить протокол на почту</h2>
      <button type="button" class="ghost icon" id="sclose" aria-label="Закрыть">${icon("close", 15)}</button></header>
    <div class="body">`;
  const box = document.getElementById("sendbox");
  box.hidden = false;
  if (done) {
    // не всем — это предупреждение, а не успех: свой знак и цвет, не зелёная галочка
    box.innerHTML = head + `<div class="sdone" role="status">
        <p class="${done.refused.length ? "sok part" : "sok"}">${done.refused.length
          ? `${icon("alert", 17)}<b>Протокол отправлен не всем</b>` : `${icon("ok", 17)}<b>Протокол отправлен</b>`}</p>
        <p>Протокол записи «${esc(name)}» ушёл на ${done.to.length} ${
          done.to.length === 1 ? "адрес" : done.to.length < 5 ? "адреса" : "адресов"}${
          done.to.length > 1 ? ", каждому отдельным письмом" : ""}: ${done.to.map(esc).join(", ")}.</p>
        ${done.refused.length ? `<p class="msg err">${icon("alert", 14)} Не ушло: ${
          done.refused.map(r => `${esc(r.email)} (${esc(r.why)})`).join("; ")}. Проверьте ${
          done.refused.length === 1 ? "адрес" : "адреса"} и отправьте ещё раз — только ${
          done.refused.length === 1 ? "ему" : "им"}.</p>` : ""}
      </div>
      <div class="row"><button type="button" id="scancel">Закрыть</button></div></div></form></div>`;
    document.getElementById("scancel").focus();
    return;
  }
  const hint = `Через запятую или с новой строки. Всего не больше ${max} получателей; каждому уйдёт отдельное
    письмо — адресов друг друга получатели не видят.` + (domains.length
      ? ` Принимаются только адреса на ${domains.map(d => "@" + esc(d)).join(", ")}.` : "");
  const format = (value, title, text, checked) => `<label class="check"><input type="radio" name="sformat"
      value="${value}" ${checked ? "checked" : ""}><span><b>${title}</b> <span class="dim">${text}</span></span></label>`;
  box.innerHTML = head + `
      <p class="klead">Запись «${esc(name)}». Протокол уйдёт за пределы сервиса — проверьте адреса.
        Кому и что отправлено, записывается в журнал действий.</p>
      ${people.length ? `<fieldset class="sgroup"><legend>Кому открыта папка записи</legend>
        ${people.map(p => `<label class="check"><input type="checkbox" name="sperson" value="${esc(p.email)}">
          <span><b>${esc(p.name)}</b> <span class="dim">${esc(p.email)}</span></span></label>`).join("")}</fieldset>` : ""}
      ${formField("sto", people.length ? "Другие адреса" : "Получатели",
        `<textarea id="sto" rows="3" autocomplete="off" autocapitalize="off" spellcheck="false"
           aria-describedby="sto-hint" placeholder="ivanov@example.ru, petrov@example.ru"></textarea>`, hint, !people.length)}
      <fieldset class="sgroup"><legend>Вложение</legend>
        ${format("pdf", "PDF", "для чтения и печати", true)}
        ${format("docx", "DOCX", "документ Word, можно править", false)}
        ${format("none", "Без вложения", "только ссылка на запись в сервисе", false)}
      </fieldset>
      ${formField("snote", "Приписка", `<textarea id="snote" rows="3" maxlength="${noteMax}"
           aria-describedby="snote-hint"></textarea>`, "Необязательно. Попадёт в письмо как есть, от вашего имени.")}
      <div class="msg" id="smsg" role="alert"></div>
      <div class="row"><button id="sgo">${icon("mail", 15)}<span>Отправить</span></button>
        <button type="button" class="ghost" id="scancel">Отмена</button></div>
    </div></form></div>`;
}

function closeSend() {
  if (!sendState || sendState.busy) return;
  sendState = null;
  const box = document.getElementById("sendbox");
  box.hidden = true;
  box.innerHTML = "";
  const back = document.getElementById("vsend");
  if (back) back.focus();
}

async function runSend(e) {
  e.preventDefault();
  if (sendState.busy || sendState.done) return;
  const box = document.getElementById("sendbox");
  const typed = document.getElementById("sto"), go = document.getElementById("sgo"), msg = document.getElementById("smsg");
  const to = [...box.querySelectorAll("input[name=sperson]:checked")].map(i => i.value)
    .concat(typed.value.trim() ? [typed.value.trim()] : []);
  fieldError("sto", ""); fieldError("snote", "");
  msg.className = "msg"; msg.innerHTML = "";
  if (!to.length) {
    fieldError("sto", sendState.people.length ? "Отметьте получателей или впишите адрес" : "Впишите хотя бы один адрес");
    typed.focus();
    return;
  }
  sendState.busy = true;
  go.disabled = true;
  go.lastElementChild.textContent = "Отправляю…";
  try {
    sendState.done = await api("/api/record/send", { file: sendState.file, to: to.join(", "),
      format: box.querySelector("input[name=sformat]:checked").value, note: document.getElementById("snote").value });
    sendState.busy = false;
    drawSend();
  } catch (err) {
    sendState.busy = false;
    go.disabled = false;
    go.lastElementChild.textContent = "Отправить";
    // отказ по адресам и по приписке встаёт под своим полем, остальное (почтовый сервер, предел) — над
    // кнопкой «Отправить», со знаком, и окно прокручивается к нему: под кнопками на невысоком экране его не видно
    const field = { to: "sto", note: "snote" }[err.field];
    if (field && fieldError(field, err.message)) document.getElementById(field).focus();
    else {
      msg.className = "msg err";
      msg.innerHTML = icon("alert", 14) + `<span>Не отправлено: ${esc(err.message)}</span>`;
      msg.scrollIntoView({ block: "nearest" });
    }
  }
}

/* ---------- ключи доступа ----------
   Человек выпускает ключ для своего ИИ-клиента (Cursor, Claude Code, Codex): имя, срок.
   Сам ключ сервис показывает один раз — сразу после выпуска; дальше в списке только его начало.
   Администратор сервиса может выпустить ключ «с управлением»: им клиент ещё и заводит
   пользователей и раздаёт права — поэтому флажок с предупреждением и срок не дольше 90 дней.
   Окно открывается с любой страницы: из меню учётной записи, из справки, по адресу …#keys. */
// { keys, canManage, days: сроки по видам ключа, manage: флажок «с управлением» в форме,
//   fresh: только что выпущенный ключ, more: форму выпуска показать и рядом с ним,
//   asking: id ключа, про который спросили «отозвать?», back: куда вернуть фокус }
let keysState = null;

const keyDay = sec => sec ? new Date(sec * 1000).toLocaleDateString("ru-RU") : "";
const KEY_TERMS = { 0: "Без срока", 7: "7 дней", 30: "30 дней", 90: "90 дней", 365: "Год" };
// что предложить первым: обычному ключу — год, ключу с управлением — месяц
const KEY_FIRST = { plain: 365, manage: 30 };
const keyKindTag = k => k.kind === "manage"
  ? `<span class="tag warn" title="Этим ключом можно управлять сервисом: пользователи, права, папки">с управлением</span>` : "";

function keyRow(k) {
  const now = Date.now() / 1000, dead = k.expires && k.expires < now;
  const asking = keysState.asking === k.id;
  return `<li class="keyrow${dead ? " dead" : ""}">
    <div class="kmain"><b>${esc(k.name)}</b>${keyKindTag(k)}<code>${esc(k.prefix)}…</code></div>
    <div class="kmeta dim">выпущен ${keyDay(k.created)} · ${k.used ? "использован " + keyDay(k.used) : "ещё не использован"} ·
      ${dead ? `<span class="kdead">срок вышел ${keyDay(k.expires)}</span>` : k.expires ? "до " + keyDay(k.expires) : "без срока"}</div>
    <div class="kact">${asking
      ? `<span>Отозвать? Клиент с этим ключом потеряет доступ.</span>
         <button type="button" class="ghost" data-keep>Оставить</button>
         <button type="button" class="danger" data-revoke="${esc(k.id)}">Отозвать</button>`
      : `<button type="button" class="ghost dangerlink" data-ask="${esc(k.id)}" aria-label="Отозвать ключ «${esc(k.name)}»">Отозвать</button>`}</div>
  </li>`;
}

function keyTerms() {
  const kind = keysState.manage ? "manage" : "plain";
  return keysState.days[kind].slice().sort((a, b) => (b === KEY_FIRST[kind]) - (a === KEY_FIRST[kind]) || (a || 1e9) - (b || 1e9))
    .map(d => `<option value="${d}">${KEY_TERMS[d] || d + " дней"}</option>`).join("");
}

function drawKeys() {
  const box = document.getElementById("keys");
  const { keys, fresh } = keysState;
  const freshBlock = fresh ? `<div class="keyfresh" role="status">
      <b>Ключ «${esc(fresh.name)}» выпущен. Скопируйте его сейчас — второй раз сервис его не покажет.</b>
      <label for="kvalue">Ключ</label>
      <div class="copyrow"><input type="text" id="kvalue" readonly value="${esc(fresh.key)}">
        <button type="button" id="kcopy">${icon("clip", 15)}<span>Копировать</span></button></div>
      <label for="kline">Строка для файла <code>.podyachiy</code> в домашней папке</label>
      <div class="copyrow"><input type="text" id="kline" readonly value="key=${esc(fresh.key)}">
        <button type="button" class="ghost" id="klinecopy">${icon("clip", 15)}<span>Копировать</span></button></div>
      <div class="msg" id="kcopied" aria-live="polite"></div>
    </div>` : "";
  box.innerHTML = `<div class="veil" id="kveil">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="ktitle">
    <header><h2 id="ktitle">Ключи доступа</h2>
      <button type="button" class="ghost icon" id="kclose" aria-label="Закрыть">${icon("close", 15)}</button></header>
    <div class="body">
      <p class="klead">Ключ нужен ИИ-клиенту — Cursor, Claude Code, Codex, — чтобы находить ваши протоколы
        и загружать записи от вашего имени. Он видит то же, что и вы; войти с ним на страницу, удалить
        запись или выпустить другой ключ нельзя. <a href="/help#ai">Как подключить клиента</a> ·
        <a href="/api">Методы API для своей программы</a></p>
      ${freshBlock}
      ${fresh && !keysState.more ? `<div class="row"><button type="button" class="ghost" id="kmore">Выпустить ещё один ключ</button></div>` : ""}
      <form id="kform" novalidate ${fresh && !keysState.more ? "hidden" : ""}>
        ${formField("kname", "Название ключа", `<input type="text" id="kname" maxlength="60" autocomplete="off"
            aria-describedby="kname-hint">`, "Чтобы потом узнать его в списке: «Cursor на рабочем ноутбуке»", true)}
        ${keysState.canManage ? `<label class="check kmanage"><input type="checkbox" id="kmanage" aria-describedby="kwarn"
            ${keysState.manage ? "checked" : ""}><span><b>Разрешить управление сервисом</b> — только для администратора</span></label>
          <div class="kwarn" id="kwarn" ${keysState.manage ? "" : "hidden"}>${icon("alert", 16)}<span><b>Осторожно.</b>
            Клиент с таким ключом может заводить и удалять пользователей, назначать администраторов и раздавать
            права на папки — от вашего имени и без входа на страницу. Выпускайте его только себе, на короткий срок,
            и отзовите, когда работа закончена. Срок — не дольше 90 дней. Ключи, пароли, удаление записей и папок
            остаются только на странице.</span></div>` : ""}
        <label for="kdays">Срок действия</label>
        <select id="kdays">${keyTerms()}</select>
        <div class="row"><button id="kgo">${icon("key", 15)}<span>Выпустить ключ</span></button></div>
        <div class="msg" id="kmsg" role="alert"></div>
      </form>
      <h3 class="khead">Выпущенные ключи</h3>
      ${keys.length ? `<ul class="keylist">${keys.map(keyRow).join("")}</ul>`
                    : `<p class="dim kempty">Ключей пока нет.</p>`}
    </div></div></div>`;
  box.hidden = false;
}

async function openKeys() {
  let box = document.getElementById("keys");
  if (!box) {
    box = document.createElement("div");
    box.id = "keys";
    box.hidden = true;
    document.body.appendChild(box);
    box.addEventListener("click", onKeysClick);
    box.addEventListener("submit", issueKey);
    box.addEventListener("change", e => {
      if (e.target.id !== "kmanage") return;
      // вид ключа сменили: другие сроки и предупреждение; набранное название остаётся
      keysState.manage = e.target.checked;
      document.getElementById("kwarn").hidden = !keysState.manage;
      document.getElementById("kdays").innerHTML = keyTerms();
    });
    box.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); closeKeys(); } });
  }
  const back = document.activeElement;
  let got;
  try { got = await api("/api/keys"); }
  catch (e) { alert("Список ключей не получен: " + e.message); return; }
  keysState = { keys: got.keys, canManage: got.can_manage, days: got.days, manage: false,
                fresh: null, more: false, asking: null, back };
  drawKeys();
  document.getElementById("kname").focus();
}

function closeKeys() {
  if (!keysState) return;
  const back = keysState.back;
  keysState = null;
  const box = document.getElementById("keys");
  box.hidden = true;
  box.innerHTML = "";       // выпущенный ключ со страницы уходит вместе с окном
  if (location.hash === "#keys") history.replaceState(history.state, "", location.pathname + location.search);
  if (back && back.isConnected && back !== document.body) back.focus();
  else document.getElementById("keysbtn")?.focus();
}

async function onKeysClick(e) {
  const hit = sel => e.target.closest(sel);
  if (e.target.id === "kveil" || hit("#kclose")) return closeKeys();
  if (hit("#kcopy") || hit("#klinecopy")) {
    const field = document.getElementById(hit("#kcopy") ? "kvalue" : "kline");
    const ok = await copyText(field.value);
    const note = document.getElementById("kcopied");
    note.className = "msg " + (ok ? "ok" : "err");
    note.textContent = ok ? (hit("#kcopy") ? "Ключ скопирован." : "Строка скопирована — вставьте её в файл .podyachiy.")
                          : "Не удалось скопировать — выделите текст и скопируйте вручную.";
    if (!ok) field.select();
    return;
  }
  if (hit("#kmore")) { keysState.more = true; drawKeys(); document.getElementById("kname").focus(); return; }
  if (hit("[data-ask]")) {
    keysState.asking = hit("[data-ask]").dataset.ask;
    drawKeys();
    document.querySelector("#keys [data-keep]").focus();     // случайный Enter ничего не отзовёт
    return;
  }
  if (hit("[data-keep]")) { keysState.asking = null; drawKeys(); return; }
  if (hit("[data-revoke]")) {
    const btn = hit("[data-revoke]");
    btn.disabled = true;
    try { keysState.keys = (await api("/api/keys/revoke", { id: btn.dataset.revoke })).keys; }
    catch (err) { alert("Ключ не отозван: " + err.message); }
    keysState.asking = null;
    drawKeys();
    (document.getElementById("kmore") || document.getElementById("kname")).focus();
  }
}

async function issueKey(e) {
  e.preventDefault();
  const name = document.getElementById("kname"), go = document.getElementById("kgo"), msg = document.getElementById("kmsg");
  fieldError("kname", "");
  if (!name.value.trim()) { fieldError("kname", "Назовите ключ — по названию вы узнаете его в списке"); name.focus(); return; }
  go.disabled = true;
  msg.className = "msg"; msg.textContent = "Выпускаю…";
  try {
    const done = await api("/api/keys", { name: name.value, days: +document.getElementById("kdays").value,
                                          manage: keysState.manage });
    keysState.keys = done.keys;
    keysState.fresh = { name: done.issued.name, key: done.key };
    keysState.more = false;
    keysState.manage = false;
    drawKeys();
    const field = document.getElementById("kvalue");
    field.focus(); field.select();
  } catch (err) {
    go.disabled = false;
    msg.textContent = "";
    fieldError("kname", err.message);
    name.focus();
  }
}

/* ---------- права на папку ----------
   Таблица: пользователь, справа три права по возрастанию — чтение, загрузка и
   администрирование. У каждого права своя область: нет, только эта папка, папка с
   подпапками. Право, пришедшее с папки выше вместе с подпапками, здесь показано, но
   меняется там, где выдано. Загрузка — отдельно от чтения: обработка записи платная.
   На узком экране три столбца не помещаются: строка складывается в карточку, и название
   права (data-th) встаёт над своим полем. */
const SCOPES = [["none", "нет"], ["only", "только эта папка"], ["tree", "папка с подпапками"]];
const RIGHTS = { read: "Чтение", upload: "Загрузка", manage: "Администрирование" };

function scopeCell(kind, u, editable, wholeTree) {
  if (u.admin) return `<span class="dim">администратор сервиса</span>`;
  const via = u[kind + "_via"];
  const options = SCOPES.filter(([v]) => !wholeTree || v !== "only");
  const label = (SCOPES.find(([v]) => v === u[kind]) || SCOPES[0])[1];
  const control = editable
    ? `<select data-kind="${kind}" data-login="${esc(u.login)}" aria-label="${
        RIGHTS[kind]}: ${esc(u.name)}">${options.map(([v, t]) =>
        `<option value="${v}" ${v === u[kind] ? "selected" : ""}>${t}</option>`).join("")}</select>`
    : `<span>${label}</span>`;
  return control + (via ? `<div class="dim via">уже есть с подпапками — от «${esc(via)}»</div>` : "");
}

function accessTable(users, path, canManage) {
  if (!users.length) return `<div class="dim upnote">Пользователей пока нет — заведите их в разделе «Пользователи».</div>`;
  const whole = path === "";          // «все папки»: только чтение целиком
  // администратору сервиса можно всё: одна ячейка на все права, а не три одинаковые
  const cells = u => u.admin
    ? `<td colspan="${whole ? 1 : 3}"><span class="dim">администратор сервиса — ему открыто всё</span></td>`
    : `<td data-th="чтение">${scopeCell("read", u, true, whole)}</td>
      ${whole ? "" : `<td data-th="загрузка">${scopeCell("upload", u, true, false)}</td>
      <td data-th="администрирование">${scopeCell("manage", u, canManage, false)}</td>`}`;
  return `<div class="scroll"><table class="acl"><thead><tr>
      <th>пользователь</th><th>чтение</th>${whole ? "" : "<th>загрузка</th><th>администрирование</th>"}</tr></thead><tbody>` +
    users.map(u => `<tr>
      <td><b>${esc(u.name)}</b><div class="dim">${esc(u.login)}</div></td>
      ${cells(u)}</tr>`).join("") +
    `</tbody></table></div>
    <div class="note" style="margin-top:10px">${whole
      ? `<b>Чтение</b> — видеть записи, протоколы, поиск и поручения во всех папках. Загрузку и
         администрирование на все папки сразу не дают: их выдают по папкам.`
      : `Три ступени, каждая включает предыдущую. <b>Чтение</b> — видеть записи, протоколы, поиск и поручения,
      скачивать протокол; загружать нельзя. <b>Загрузка</b> — ещё и класть сюда новые записи: обработка каждой
      стоит денег, поэтому это право выдаётся отдельно. <b>Администрирование</b> — ещё и раздавать чтение и
      загрузку, подписывать говорящих и пересобирать протокол; с областью «папка с подпапками» — заводить
      подпапки.${canManage ? "" : " Администраторов папок назначает администратор сервиса."}`}</div>`;
}

/* Смена права в таблице: отправляем одно изменение и возвращаем свежий список. */
async function saveScope(select, path) {
  const d = await api("/api/access", { dir: path, login: select.dataset.login, [select.dataset.kind]: select.value });
  return d;
}

/* ---------- левое меню ----------
   Одно и то же на всех страницах: записи и папки сверху, управление сервисом под ними,
   справка и учётная запись внизу. Раздел управления — пункты этого же меню, а не отдельная
   страница со своей навигацией: вернуться к записям всегда можно тем же меню. */
let sidePage = "", sideDirs = [], sideFolder = "";

function initSidebar(page) {
  sidePage = page;
  document.getElementById("side").innerHTML = `
    <a class="brand" href="/"></a>
    ${page === "records" ? `<button id="upbtn" disabled hidden aria-describedby="upwhy"></button>
      <div class="fhint" id="upwhy" hidden></div><input type="file" id="upfile" multiple hidden>` : ""}
    <nav id="nav" aria-label="Разделы"></nav>
    <div class="foot">
      <span class="pill" id="livepill" hidden></span>
      <div id="footnav"></div>
      <div class="account"><div id="account"></div>
        <button class="ghost icon" id="theme" title="Светлая или тёмная тема" aria-label="Переключить тему"></button></div>
      <div id="accountacts"></div>
    </div>`;
}

function renderNav(dirs, folder) {
  if (dirs) sideDirs = dirs;
  if (folder !== undefined) sideFolder = folder;
  const records = sidePage === "records";
  const pane = sidePage === "admin" ? (location.hash.slice(1) || "records") : "";
  const item = (path, label, ic, extra) => {
    const on = records && (path === "" ? !sideFolder : (sideFolder === path || sideFolder.startsWith(path + "/")));
    return `<a class="nav${on ? " on" : ""}" ${on ? 'aria-current="page"' : ""} href="${
      path ? "/?path=" + encodeURIComponent(path) : "/"}" data-go="${esc(path)}">${
      icon(ic, 17)}<span>${esc(label)}</span>${extra || ""}</a>`;
  };
  // «Настройки» — один пункт на два раздела страницы: шаблоны и оформление (вкладки внутри)
  const manage = (id, label, ic, extra, also) => {
    const on = pane === id || (also || []).includes(pane);
    return `<a class="nav${on ? " on" : ""}" ${on ? 'aria-current="page"' : ""} href="/admin#${id}">${
      icon(ic, 17)}<span>${label}</span>${extra || ""}</a>`;
  };
  // сколько заявок на доступ ждёт решения — видно только администратору сервиса
  const waiting = me && me.admin && me.requests
    ? `<span class="count" aria-label="заявок на доступ: ${me.requests}">${me.requests}</span>` : "";
  const home = me && me.home, homeRoot = me && me.home_root;
  const shared = sideDirs.filter(d => d.name !== homeRoot);
  const everyone = me && me.admin && sideDirs.some(d => d.name === homeRoot);   // администратор видит папки всех
  document.getElementById("nav").innerHTML =
    `<h3>Записи</h3>` + item("", "Все записи", "home") +
    `<a class="nav${sidePage === "tasks" ? " on" : ""}" ${sidePage === "tasks" ? 'aria-current="page"' : ""
      } href="/tasks">${icon("tasks", 17)}<span>Поручения</span></a>` +
    (home ? item(home, "Моя папка", "folder") : "") +
    shared.map(d => item(d.path, d.name, "folder", `<span class="n">${d.files || ""}</span>`)).join("") +
    (everyone ? item(homeRoot, "Папки пользователей", "users") : "") +
    (me && me.admin ? `<h3>Управление</h3>` + manage("records", "Обработка", "clock") +
      (me.personal ? "" : manage("folders", "Папки и доступ", "folder") + manage("users", "Пользователи", "users", waiting)) +
      manage("costs", "Расходы", "coin") +
      manage("templates", "Настройки", "gear", "", ["brand"]) : "");
  const foot = (page, href, label, ic) => `<a class="nav${sidePage === page ? " on" : ""}" ${
    sidePage === page ? 'aria-current="page"' : ""} href="${href}">${icon(ic, 17)}<span>${label}</span></a>`;
  document.getElementById("footnav").innerHTML =
    foot("help", "/help", "Справка", "list") + foot("about", "/about", "О сервисе", "info");
}

/* На страницах без своего списка записей (управление, справка) меню само узнаёт
   папки и состояние сервиса. */
async function loadSidebar() {
  try {
    const d = await api("/api/browse");
    renderNav(d.dirs);
    showService(d.service);
  } catch (e) { renderNav([]); }
}

function showService(s) {
  const pill = document.getElementById("livepill");
  if (!pill) return;
  pill.hidden = false;
  // s.running — сколько записей в работе: обработчиков несколько, и идут они одновременно
  pill.className = "pill" + (s.running ? " busy" : "");
  pill.innerHTML = `<span class="dot"></span>` + (s.running
    ? (s.running > 1 ? `в работе ${s.running}` + (s.queue ? `, в очереди ${s.queue}` : "")
      : "обрабатывается запись" + (s.queue ? `, в очереди ещё ${s.queue}` : ""))
    : (s.queue || s.watching ? `в очереди ${s.queue + s.watching}` : "новых записей нет"));
}

// ---------- тема ----------
function applyTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved) document.documentElement.setAttribute("data-theme", saved);
  else document.documentElement.removeAttribute("data-theme");
  const dark = saved === "dark" ||
    (!saved && matchMedia("(prefers-color-scheme: dark)").matches);
  const btn = document.getElementById("theme");
  if (btn) btn.innerHTML = icon(dark ? "sun" : "moon", 16);
}

function initTheme() {
  initBrand();
  const btn = document.getElementById("theme");
  if (btn) btn.addEventListener("click", () => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark" ||
      (!localStorage.getItem(THEME_KEY) && matchMedia("(prefers-color-scheme: dark)").matches);
    localStorage.setItem(THEME_KEY, dark ? "light" : "dark");
    applyTheme();
  });
  applyTheme();
}

/* ---------- просмотр готовых результатов ----------
   Открывается по клику на записи и на странице обзора, и в истории
   администрирования. Это чтение, поэтому доступно всем: пароль закрывает
   управление, а не результаты. */

const KINDS = [["summary", "Протокол"], ["transcript", "Расшифровка с говорящими"],
               ["srt", "Субтитры"]];
let viewFile = null, viewKind = "summary", viewText = "", lastFocus = null;
let viewAudio = false;      // у этой записи сохранён звук — значит метки времени кликабельны
let viewData = null;        // ответ /api/result: текст, а для расшифровки — части и реплики с подписями
let namesOpen = false, namesDraft = {}, lineEdit = null;
let dueEdit = null;         // ключ поручения, у которого в окне записи открыто поле срока
let prevAsk = false;        // у раздела «Поручения прошлого совещания» открыт вопрос «убрать?»

// Минимальный разбор Markdown: заголовки, списки, таблицы, цитаты, жирный,
// код. Внешние библиотеки не подключить — страница работает без интернета.
/* Метка [чч:мм:сс] в протоколе — это ссылка на момент записи: клик перематывает
   плеер туда. Пока звука нет, метка остаётся обычным текстом: ссылка, которая
   никуда не ведёт, хуже её отсутствия. */
function tsLinks(html) {
  if (!viewAudio) return html;
  return html.replace(/\[(\d{1,2}):([0-5]\d):([0-5]\d)\]/g, (all, h, m, sec) =>
    `<a class="ts" role="button" tabindex="0" title="Послушать с этого места"
        data-sec="${(+h) * 3600 + (+m) * 60 + (+sec)}">${all}</a>`);
}

function playFrom(sec) {
  const a = document.getElementById("vaudio");
  if (!a) return;
  const go = () => { a.currentTime = sec; a.play().catch(() => {}); };
  // preload="none": до первого клика метаданных нет, и перемотка молча не сработала бы
  if (a.readyState >= 1) go();
  else { a.addEventListener("loadedmetadata", go, { once: true }); a.load(); }
}

function mdToHtml(src) {
  let html = "", list = null, table = false, para = [];
  // [текст](адрес) — ссылка на другую запись: такие сервис ставит в разделе «Поручения прошлого
  // совещания». Адрес — только сайта; метка времени в такой ссылке не в квадратных скобках,
  // поэтому за момент этой записи её не примут.
  const inl = s => tsLinks(esc(s)
    .replace(/\[([^\]\[]+)\]\(((?:https?:\/\/|\/)[^)\s]*)\)/g, '<a class="ref" href="$2">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>"));
  const flushPara = () => { if (para.length) { html += "<p>" + inl(para.join(" ")) + "</p>"; para = []; } };
  const flushList = () => { if (list) { html += "</" + list + ">"; list = null; } };
  const flushTable = () => { if (table) { html += "</table>"; table = false; } };
  const flushAll = () => { flushPara(); flushList(); flushTable(); };

  // n — номер строки текста: по нему строка таблицы задач находит свою отметку «выполнено» (markTasks)
  for (const [n, raw] of src.replace(/\r/g, "").split("\n").entries()) {
    const line = raw.trimEnd();
    if (!line.trim()) { flushAll(); continue; }
    if (line.trim().startsWith("<!--")) continue;           // служебный заголовок файла
    if (line.trim().startsWith("|")) {
      const cells = line.trim().replace(/^\||\|$/g, "").split("|").map(s => s.trim());
      if (/^[-:\s]+$/.test(cells.join(""))) continue;       // разделитель шапки
      flushPara(); flushList();
      if (!table) {
        html += "<table><tr>" + cells.map(c => `<th>${inl(c)}</th>`).join("") + "</tr>";
        table = true;
      } else if (/^\*\*[^*]+\*\*$/.test(cells[0]) && cells.slice(1).every(c => !c)) {
        // жирная строка с пустыми ячейками — имя ответственного над его задачами
        html += `<tr class="who"><td colspan="${cells.length}">${inl(cells[0])}</td></tr>`;
      } else {
        html += `<tr data-ln="${n}">` + cells.map(c => `<td>${inl(c)}</td>`).join("") + "</tr>";
      }
      continue;
    }
    flushTable();
    let m;
    if (m = line.match(/^(#{1,6})\s+(.*)$/)) {
      flushAll();
      const lvl = Math.min(m[1].length, 3);
      html += `<h${lvl}>${inl(m[2])}</h${lvl}>`;
    } else if (/^\s*(---|___|\*\*\*)\s*$/.test(line)) {
      flushAll(); html += "<hr>";
    } else if (m = line.match(/^\s*>\s?(.*)$/)) {
      flushAll(); html += `<blockquote>${inl(m[1])}</blockquote>`;
    } else if (m = line.match(/^\s*[-*+]\s+(.*)$/)) {
      flushPara();
      if (list !== "ul") { flushList(); html += "<ul>"; list = "ul"; }
      html += `<li>${inl(m[1])}</li>`;
    } else if (m = line.match(/^\s*\d+[.)]\s+(.*)$/)) {
      flushPara();
      if (list !== "ol") { flushList(); html += "<ol>"; list = "ol"; }
      html += `<li>${inl(m[1])}</li>`;
    } else {
      flushList(); para.push(line.trim());
    }
  }
  flushAll();
  return html;
}

function drawViewer(body, ready) {
  const voicesHere = ready && viewKind !== "summary" && hasVoices();
  document.getElementById("viewer").innerHTML = `
    <div class="veil" id="veil"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="vtitle">
      <header>
        <div class="title">
          <div class="t path" id="vtitle">${esc(viewFile)}</div>
          <div class="acts">
            ${voicesHere ? `<button class="ghost copybtn" id="vnames"
              aria-expanded="${namesOpen}">${icon("users", 13)}Говорящие</button>` : ""}
            ${ready ? `<button class="ghost copybtn" id="vdict" aria-haspopup="dialog"
              title="Участники и сокращения этой записи">${icon("list", 13)}Словарь</button>` : ""}
            ${ready && viewData && viewData.has_map ? `<details class="dl" id="vdl">
                <summary class="copybtn" title="Сохранить запись файлом">${icon("copy", 13)}Скачать</summary>
                <div class="dlmenu">
                  <a href="/api/export?format=docx&amp;file=${encodeURIComponent(viewFile)}"><b>DOCX</b><span>протокол документом Word</span></a>
                  <a href="/api/export?format=pdf&amp;file=${encodeURIComponent(viewFile)}"><b>PDF</b><span>протокол для печати и пересылки</span></a>
                  <a href="/api/map?download=1&amp;file=${encodeURIComponent(viewFile)}"><b>HTML</b><span>карта, протокол и расшифровка: открывается без сервиса</span></a>
                </div>
              </details>${viewData.can_send ? `<button class="ghost copybtn" id="vsend"
                title="Отправить протокол на почту: файлом или ссылкой">${icon("mail", 13)}Отправить</button>` : ""}` : ""}
            <button class="ghost copybtn" id="vcopy" ${ready ? "" : "disabled"}>${icon("clip", 13)}Копировать</button>
            <button class="ghost icon" id="vclose" aria-label="Закрыть">${icon("close", 15)}</button>
          </div>
        </div>
        ${ready && viewData && viewData.can_act && onRecordAction ? `<div class="recacts" role="group" aria-label="Действия с записью">
          <button class="ghost copybtn" data-recact="rename">Переименовать</button>
          <button class="ghost copybtn" data-recact="move">Перенести</button>
          <button class="ghost copybtn dangerlink" data-recact="delete">Удалить</button></div>` : ""}
        <div class="tabs" role="tablist">${KINDS.map(([k, title]) =>
          `<button data-kind="${k}" role="tab" aria-selected="${k === viewKind}">${title}</button>` +
          // «Карта» — между протоколом и расшифровкой, в том же порядке, что виды на её странице;
          // это ссылка: карта — отдельная страница, вернуться с неё — кнопка «К записи» в её шапке
          (k === "summary" && ready && viewData && viewData.has_map ? `<a class="tablink" role="tab" aria-selected="false"
            href="/api/map?file=${encodeURIComponent(viewFile)}" title="Карта записи: дерево разделов протокола">Карта</a>` : "")).join("")}</div>
        ${viewAudio ? `<audio id="vaudio" class="vaudio" controls preload="none"
          src="/api/audio?file=${encodeURIComponent(viewFile)}">Браузер не умеет проигрывать этот файл.</audio>` : ""}
        <div class="tsaid" id="vlive" role="status" aria-live="polite"></div>
      </header>
      <div class="doc">${body}</div>
    </div></div>`;
  const close = document.getElementById("vclose");
  close.addEventListener("click", () => closeViewer());
  // Клик считается кликом по подложке, только если на ней и нажали: выделяя текст
  // в поле мышью, кнопку легко отпустить за краем окна, и окно закрывалось вместе
  // со всем введённым.
  const veil = document.getElementById("veil");
  let downOnVeil = false;
  veil.addEventListener("mousedown", e => { downOnVeil = e.target === veil; });
  veil.addEventListener("click", e => {
    if (e.target === veil && downOnVeil) closeViewer();
  });
  document.querySelectorAll(".tabs button[data-kind]").forEach(b =>
    b.addEventListener("click", () => openViewer(viewFile, b.dataset.kind)));
  document.querySelector(".doc").addEventListener("click", onDocClick);
  document.querySelector(".doc").addEventListener("change", onTaskMark);
  document.querySelectorAll("[data-recact]").forEach(b =>
    b.addEventListener("click", () => onRecordAction(b.dataset.recact)));
  const who = document.getElementById("vnames");
  if (who) who.addEventListener("click", () => toggleNames(!namesOpen));
  const words = document.getElementById("vdict");
  if (words) words.addEventListener("click", () => openDict({ file: viewFile }));
  const send = document.getElementById("vsend");
  if (send) send.addEventListener("click", openSend);
  // меню «Скачать»: закрывается выбором, щелчком мимо и клавишей Escape (окно записи при этом остаётся)
  const dl = document.getElementById("vdl");
  if (dl) {
    dl.addEventListener("click", e => { if (e.target.closest("a")) dl.open = false; });
    veil.addEventListener("click", e => { if (!dl.contains(e.target)) dl.open = false; });
    dl.addEventListener("keydown", e => {
      if (e.key !== "Escape" || !dl.open) return;
      e.stopPropagation();
      dl.open = false;
      dl.querySelector("summary").focus();
    });
  }
  const copy = document.getElementById("vcopy");
  if (ready) copy.addEventListener("click", async () => {
    const ok = await copyText(viewText);
    copy.classList.toggle("done", ok);
    copy.innerHTML = icon(ok ? "ok" : "alert", 13) + (ok ? "Скопировано" : "Не вышло");
    setTimeout(() => {
      copy.classList.remove("done");
      copy.innerHTML = icon("clip", 13) + "Копировать";
    }, 1800);
  });
  close.focus();
}

function onDocClick(e) {
  const t = e.target;
  const mark = t.closest("a.ts");
  if (mark) playFrom(+mark.dataset.sec);
  const voice = t.closest("[data-play]");
  if (voice) playFrom(+voice.dataset.play);
  const line = t.closest("button.who");
  if (line) { lineEdit = lineEdit === line.dataset.line ? null : line.dataset.line; fillDoc(); focusIn(".lned input"); }
  const carry = t.closest("[data-carry]");
  if (carry) carryNames(+carry.dataset.carry);
  if (t.closest("#nsave")) saveNames();
  if (t.closest("#nclose")) toggleNames(false);
  if (t.closest("#nopen")) toggleNames(true);
  if (t.closest("#lnsave")) saveLine(false);
  if (t.closest("#lnreset")) saveLine(true);
  if (t.closest("#lncancel")) { lineEdit = null; fillDoc(); }
  if (t.closest("#rebuild")) rebuild();
  const due = t.closest("[data-due]");
  if (due) { dueEdit = due.dataset.due; fillDoc(); focusIn("#duein"); }
  if (t.closest("[data-duesave]") && dueTyped()) saveDue(dueTyped());
  if (t.closest("[data-dueclear]")) saveDue("");
  if (t.closest("[data-duecancel]")) closeDue();
  if (t.closest("#prevdrop")) { prevAsk = true; fillDoc(); focusIn("#prevno"); }
  if (t.closest("#prevno")) { prevAsk = false; fillDoc(); focusIn("#prevdrop"); }
  if (t.closest("#prevyes")) setPrevious(false);
  if (t.closest("#prevback")) setPrevious(true);
}

/* Страница может следить за тем, что открыто, — например, держать это в адресе.
   Хук зовётся с "open" при первом открытии, "kind" при смене вкладки и "close".
   Когда просмотрщик открывают в ответ на изменение адреса, зовём с silent —
   иначе страница снова полезла бы править историю, из которой всё и пришло. */
let onViewerChange = null;
/* Переименовать, перенести, удалить запись: кнопки появляются у хозяина записи на той
   странице, которая умеет эти действия показать, — она и задаёт обработчик. */
let onRecordAction = null;

async function openViewer(file, kind, silent) {
  const wasOpen = !!viewFile;
  if (!viewFile) lastFocus = document.activeElement;
  viewFile = file; viewKind = kind || "summary"; viewText = ""; viewAudio = false;
  viewData = null; lineEdit = null; dueEdit = null; prevAsk = false;
  if (onViewerChange && !silent) onViewerChange(wasOpen ? "kind" : "open");
  drawViewer(`<div class="skeleton" aria-label="загружаю">
    <span style="width:45%"></span><span></span><span></span><span style="width:70%"></span>
    <span style="width:88%"></span><span style="width:60%"></span></div>`, false);
  try {
    viewData = await api(`/api/result?file=${encodeURIComponent(file)}&kind=${viewKind}`);
    viewAudio = !!viewData.audio;
    drawViewer("", true);
    fillDoc();
  } catch (e) {
    drawViewer(`<p class="msg err">${esc(e.message)}</p>`, false);
  }
}

/* ---------- кто есть кто ----------
   На диске расшифровка хранится с метками S1, S2 — как её отдала модель
   распознавания. Кто за метками, решает сервер: подпись у реплики, иначе
   список её части, иначе догадка разбора с меткой («Иван (S2)»).
   Подписывают в два слоя: сначала общий список — отдельно по каждой части
   записи, потому что после стыка модель раздаёт метки заново, — потом
   точечно, у отдельных реплик. Протокол пересобирается по кнопке: правка
   каждой строки иначе стоила бы протокола. */

const clock = s => {
  const t = Math.max(Math.floor(s || 0), 0);
  return [Math.floor(t / 3600), Math.floor(t % 3600 / 60), t % 60]
    .map(n => String(n).padStart(2, "0")).join(":");
};
const share = v => v < 0.01 ? "<1%" : Math.round(v * 100) + "%";
const hasVoices = () => !!(viewData && (viewData.parts || []).some(p => p.speakers.length));
const focusIn = sel => { const el = document.querySelector(sel); if (el) el.focus(); };

function voices(n) {
  const a = n % 100, b = n % 10;
  if (b === 1 && a !== 11) return "голос";
  return b >= 2 && b <= 4 && (a < 12 || a > 14) ? "голоса" : "голосов";
}

// Подписывать ФИО и пересобирать протокол может хозяин папки. Остальным поля и кнопки
// не показываются вовсе: говорящие — списком, и одна строка о том, к кому идти.
// Пересборка платная: хозяину она открыта, только если в этой папке у него есть право загрузки.
const canSign = () => !!(viewData && viewData.can_edit);
const canRebuild = () => !!(viewData && viewData.can_rebuild);
const REBUILD_BY = "Пересобрать протокол может тот, у кого есть право загрузки в эту папку: пересборка платная";
const SIGN_BY = "Подписывать может владелец папки или её администратор";

function say(id, kind, text) {
  const el = document.getElementById(id);
  if (el) { el.className = "msg " + kind; el.textContent = text; }
}

/* Протокол строится по подписям на момент сборки. Поправили подписи после —
   протокол с ними расходится, и об этом надо сказать на любой вкладке. */
function staleBar() {
  if (!viewData) return "";
  if (viewData.rebuilding) return `<div class="nhint busy">${icon("clock", 14)}
    <span>Протокол пересобирается — откройте его заново через несколько минут</span></div>`;
  if (!viewData.names_newer && !viewData.dict_newer) return "";
  const what = viewData.names_newer && viewData.dict_newer ? "ФИО и словарь записи правили"
    : viewData.dict_newer ? "Словарь записи правили" : "ФИО правили";
  return `<div class="nhint warn">${icon("alert", 14)}
    <span>${what} после того, как собран протокол, — в нём этих правок ещё нет</span>
    ${canRebuild() ? `<button class="copybtn" id="rebuild">Пересобрать протокол</button>
    <div class="msg" id="rbmsg" role="status"></div>` : canSign() ? `<span class="dim">${REBUILD_BY}</span>` : ""}</div>`;
}

function namesHint() {
  if (namesOpen || !hasVoices() || viewKind === "summary") return "";
  const parts = viewData.parts;
  const inLists = parts.reduce((n, p) => n + p.speakers.filter(s => s.manual).length, 0);
  const inLines = (viewData.lines || []).filter(l => l.line).length;
  const total = parts.reduce((n, p) => n + p.speakers.length, 0);
  const text = inLists || inLines
    ? `Подписано вручную: ${inLists} в списках${inLines ? `, отдельных реплик ${inLines}` : ""}`
    : `Говорящие не подписаны: ${total} ${voices(total)}${parts.length > 1 ? ` в ${parts.length} частях записи` : ""}`;
  if (!canSign()) return `<div class="nhint">${icon("users", 14)}<span>${text}</span>
    <button class="ghost copybtn" id="nopen">Список говорящих</button></div>`;
  return `<div class="nhint">${icon("users", 14)}<span>${text}${viewKind === "transcript"
      ? `<br><span class="dim">Имя у реплики кликабельно — так подписывается одна строка</span>` : ""}</span>
    <button class="ghost copybtn" id="nopen">${inLists ? "Изменить ФИО" : "Подписать ФИО"}</button></div>`;
}

// ФИО для подсказки при подписи: сначала из словаря записи и её папок (с должностью), затем
// уже встречавшиеся в записи
function nameOptions() {
  const roster = viewData.roster || [], listed = new Set(roster.map(p => p.name));
  return roster.map(p => `<option value="${esc(p.name)}" label="${esc(p.role)}">`).join("") +
    (viewData.known || []).filter(n => !listed.has(n)).map(n => `<option value="${esc(n)}">`).join("");
}

function namesPanel() {
  if (!namesOpen) return "";
  const many = viewData.parts.length > 1, roster = viewData.roster || [];
  const listen = s => viewAudio && s.sample_sec != null
    ? `<button class="ghost icon" data-play="${s.sample_sec}" title="Послушать этот голос"
         aria-label="Послушать ${esc(s.label)}">${icon("play", 13)}</button>` : "<span></span>";
  // тому, кто подписывать не может, — тот же список без полей и кнопок сохранения
  if (!canSign()) return `<section class="names" aria-label="Говорящие">
    ${viewData.parts.map(p => `
      ${many ? `<div class="ph"><b>Часть ${p.index}</b>
        <span class="dim">${clock(p.start)}–${p.end == null ? "конец" : clock(p.end)}</span></div>` : ""}
      <ul class="spklist">${p.speakers.map(s => `<li class="spk">
        <span class="tag">${esc(s.label)}</span>
        ${s.manual || s.auto ? `<span class="nm">${esc(s.manual || s.auto)}${
            s.manual ? "" : ` <span class="dim">— так подписал разбор</span>`}</span>`
          : `<span class="nm dim">не подписан</span>`}
        <span class="dim num" title="доля слов${many ? " в этой части" : " в записи"}">${share(s.share)}</span>
        ${listen(s)}</li>`).join("")}</ul>`).join("")}
    <p class="dim signby">${SIGN_BY}.</p>
    <div class="row"><button class="ghost" id="nclose">Закрыть</button></div>
  </section>`;
  const body = viewData.parts.map(p => `
    ${many ? `<div class="ph"><b>Часть ${p.index}</b>
      <span class="dim">${clock(p.start)}–${p.end == null ? "конец" : clock(p.end)}</span>
      ${p.index > 1 ? `<button class="ghost copybtn" data-carry="${p.index}"
        title="Заполнить пустые поля ФИО тех же меток из части ${p.index - 1}">Перенести из части ${p.index - 1}</button>` : ""}
      </div>` : ""}
    ${p.speakers.map(s => {
      const draft = ((namesDraft[p.index] || {})[s.label]);
      return `<div class="spk">
        <span class="tag">${esc(s.label)}</span>
        <input type="text" data-part="${p.index}" data-label="${esc(s.label)}" maxlength="120"
               autocomplete="off" name="speaker-${p.index}-${esc(s.label)}" list="rosterNames"
               value="${esc(draft !== undefined ? draft : s.manual)}"
               placeholder="${esc(s.auto ? `${s.auto} — так подписал разбор` : "Фамилия Имя Отчество")}"
               aria-label="ФИО говорящего ${esc(s.label)}${many ? ` в части ${p.index}` : ""}">
        <span class="dim num" title="доля слов${many ? " в этой части" : " в записи"}">${share(s.share)}</span>
        ${listen(s)}
      </div>`;
    }).join("")}`).join("");
  return `<section class="names" aria-label="Подписи говорящих">
    <div class="note">Впишите ФИО тем, кого узнали; пустое поле — оставить подпись разбора.${many
      ? ` Запись распознана частями, и после стыка модель раздаёт метки заново: S3 в первой
         части и S3 во второй — не обязательно один человек, поэтому списки у частей свои.` : ""}
      Подпись у отдельной реплики главнее списка.${roster.length
        ? ` ФИО из словаря (${roster.length}) подсказываются при вводе.`
        : ` Чтобы ФИО подсказывались при вводе, заполните словарь — кнопка «Словарь» вверху.`}</div>
    <datalist id="rosterNames">${nameOptions()}</datalist>
    ${body}
    <div class="row">
      <button id="nsave">${canRebuild() ? "Сохранить и пересобрать протокол" : "Сохранить подписи"}</button>
      <button class="ghost" id="nclose">Закрыть</button>
    </div>
    ${canRebuild() ? "" : `<div class="fhint">${REBUILD_BY} — подписи сохранятся, протокол останется прежним.</div>`}
    <div class="msg" id="nmsg" role="status"></div>
  </section>`;
}

function lineEditor(ln) {
  const known = nameOptions();
  return `<div class="lned">
    <input type="text" list="knownNames" maxlength="120" value="${esc(ln.line ? ln.who : "")}"
           placeholder="ФИО того, кто говорит эту реплику" aria-label="ФИО для этой реплики">
    <datalist id="knownNames">${known}</datalist>
    <div class="row">
      <button class="copybtn" id="lnsave">Подписать только эту реплику</button>
      ${ln.line ? `<button class="ghost copybtn" id="lnreset">Вернуть подпись по списку</button>` : ""}
      <button class="ghost copybtn" id="lncancel">Отмена</button>
    </div>
    <div class="msg" id="lnmsg" role="status"></div>
  </div>`;
}

function transcriptHtml() {
  let part = 1;
  return `<div class="lines">` + viewData.lines.map(ln => {
    const seam = ln.part !== part
      ? `<div class="seam">${icon("alert", 13)}Часть ${ln.part} с ${clock(viewData.parts[ln.part - 1].start)}: здесь модель заново
          раздала метки — сверьте подписи ниже стыка</div>` : "";
    part = ln.part;
    return seam + `<div class="ln${ln.manual ? " signed" : ""}${ln.line ? " own" : ""}">
      ${canSign() ? `<button class="who" data-line="${esc(ln.key)}" aria-expanded="${lineEdit === ln.key}"
              title="${ln.line ? "Подписана отдельно. " : ""}Подписать эту реплику">${esc(ln.who)}</button>`
        : `<span class="who">${esc(ln.who)}</span>`}
      ${viewAudio ? `<a class="ts" role="button" tabindex="0" data-sec="${ln.start}"
          title="Послушать с этого места">${clock(ln.start)}</a>` : ""}
      <span class="tx">${esc(ln.text)}</span>
      ${canSign() && lineEdit === ln.key ? lineEditor(ln) : ""}
    </div>`;
  }).join("") + `</div>`;
}

function fillDoc() {
  const doc = document.querySelector(".doc");
  const top = doc.scrollTop;
  viewText = viewData.text;
  let body;
  if (viewKind === "summary") body = mdToHtml(viewData.text);
  else if (viewKind === "transcript" && (viewData.lines || []).length) body = transcriptHtml();
  else body = `<pre class="plain">${esc(viewData.text)}</pre>`;
  doc.innerHTML = staleBar() + namesHint() + namesPanel() + body;
  if (viewKind === "summary") { markTasks(doc); markPrevious(doc); }
  doc.scrollTop = top;
}

/* ---------- отметка «выполнено» у поручений протокола ----------
   Сервер разбирает раздел «Задачи» сам и называет строку текста, в которой стоит каждое
   поручение (line), — страница таблицу заново не разбирает, а находит строку по номеру.
   Флажок ставит хозяин записи; остальным отметка видна подписью под поручением.
   Срок: в ячейке срока (due_col) вместо слов протокола — дата, если человек её задал; хозяину
   записи ячейка — кнопка, по которой открывается поле даты. */
const markDay = at => `${at.slice(8, 10)}.${at.slice(5, 7)}.${at.slice(0, 4)}`;

/* Срок поручения — одинаково в реестре и в окне записи. Дата человека главнее слов протокола:
   рядом значок «вручную» и сами слова; просрочку видно подписью со значком, не только цветом. */
function dueView(t) {
  if (!t.due) return (t.due_text ? `<span class="duetext">${esc(t.due_text)}</span>` : "") + (t.due_date
    ? `<span class="sub" title="Срок, пересчитанный от дня совещания">≈ ${markDay(t.due_date)}</span>` : "");
  return `<span class="duedate">${markDay(t.due)}</span>` +
    `<span class="duehand" title="Срок задан вручную: ${esc(t.due_by)}, ${markDay(t.due_at)}">${icon("edit", 11)}вручную</span>` +
    (t.overdue ? `<span class="overdue">${icon("alert", 12)}просрочено</span>` : "") +
    (t.due_text ? `<span class="sub">в протоколе: ${esc(t.due_text)}</span>` : "");
}

// «Сохранить» с пустым или недописанным полем — не снятие срока: для него своя кнопка
function dueTyped() {
  const input = document.getElementById("duein");
  if (input.value) return input.value;
  document.getElementById("dueerr").textContent = "Укажите дату целиком: день, месяц и год";
  input.setAttribute("aria-invalid", "true");
  input.focus();
  return null;
}

// то же для хозяина записи: срок — кнопка, а когда её нажали — поле даты; id — чем строка названа в data-due
function dueCell(t, id, editing, canEdit) {
  if (!canEdit) return dueView(t);
  if (editing) return `<span class="dueform">
    <input type="date" id="duein" value="${t.due || t.due_date || ""}" aria-label="Срок поручения: ${esc(t.task)}">
    <button type="button" data-duesave>Сохранить</button>
    ${t.due ? `<button type="button" class="ghost" data-dueclear
      title="Убрать дату: сроком снова будут слова протокола">Как в протоколе</button>` : ""}
    <button type="button" class="ghost" data-duecancel>Отмена</button>
    <span class="ferr" id="dueerr" role="alert"></span></span>`;
  return `<button type="button" class="duebtn" data-due="${esc(id)}" title="Изменить срок"
    aria-label="Срок: ${t.due ? markDay(t.due) : esc(t.due_text || "не задан")}. Изменить">${
    dueView(t) || `<span class="dueadd">задать срок</span>`}</button>`;
}

function markTasks(doc) {
  for (const t of viewData.tasks || []) {
    const row = doc.querySelector(`tr[data-ln="${t.line}"]`);
    if (!row) continue;
    const cell = row.cells[0];
    row.classList.toggle("closed", t.done);
    if (t.done) cell.insertAdjacentHTML("beforeend", `<span class="donenote">${icon("ok", 12)}выполнено, ${
      markDay(t.done_at)} · ${esc(t.done_by)}${t.comment ? " · " + esc(t.comment) : ""}</span>`);
    if (viewData.can_mark) cell.insertAdjacentHTML("afterbegin", `<label class="tdone"><input type="checkbox"
      data-task="${t.key}"${t.done ? " checked" : ""}><span class="vh">Выполнено: ${esc(t.task)}</span></label>`);
    const due = t.due_col == null ? null : row.cells[t.due_col];
    if (due && (t.due || viewData.can_mark)) {
      due.classList.add("duecell");
      due.innerHTML = dueCell(t, t.key, dueEdit === t.key, viewData.can_mark);
    }
  }
}

function closeDue() {
  const key = dueEdit;
  dueEdit = null;
  fillDoc();
  focusIn(`[data-due="${key}"]`);
}

async function saveDue(due) {
  const key = dueEdit, live = document.getElementById("vlive");
  try {
    const got = (await api("/api/record/task", { file: viewFile, key, due })).task;
    viewData.tasks.forEach(t => { if (t.key === key) Object.assign(t, got); });
    closeDue();
    live.className = "tsaid";
    live.textContent = (got.due ? `Срок — ${markDay(got.due)}: ` : "Срок снова как в протоколе: ") + `«${got.task}»`;
  } catch (err) {
    live.className = "tsaid err";
    live.textContent = "Срок не сохранён: " + err.message;
  }
}

async function onTaskMark(e) {
  const box = e.target.closest("input[data-task]");
  if (!box) return;
  const key = box.dataset.task, done = box.checked;
  const live = document.getElementById("vlive");
  box.disabled = true;
  try {
    const got = (await api("/api/record/task", { file: viewFile, key, done })).task;
    viewData.tasks.forEach(t => { if (t.key === key) Object.assign(t, got); });
    fillDoc();
    focusIn(`input[data-task="${key}"]`);
    live.className = "tsaid";
    live.textContent = (done ? "Отмечено выполненным: " : "Отметка снята: ") + `«${got.task}»`;
  } catch (err) {
    box.checked = !done;
    box.disabled = false;
    live.className = "tsaid err";
    live.textContent = "Отметка не сохранена: " + err.message;
  }
}

/* ---------- раздел «Поручения прошлого совещания» ----------
   Раздел дописывает в конец протокола сам сервис, если папка записи — серия совещаний. Хозяин
   папки может убрать его у этой записи — кнопкой под заголовком раздела, после вопроса: раздел
   пропадёт из протокола, карты и выгрузок. Убранный раздел пересборка не возвращает; вернуть его —
   кнопкой в конце протокола. Оба действия без пересборки и без вызова модели. */
function markPrevious(doc) {
  const p = viewData.previous;
  if (!p || !viewData.can_edit) return;
  if (p.present) {
    const head = [...doc.querySelectorAll("h2")].find(h => h.textContent.trim() === p.title);
    if (!head) return;
    head.insertAdjacentHTML("afterend", prevAsk
      ? `<div class="prevbar ask" role="group" aria-labelledby="prevq">
          <p id="prevq"><b>Убрать раздел из протокола?</b> Он пропадёт из протокола, карты и выгрузок DOCX и PDF
            и не вернётся при пересборке. Вернуть его можно кнопкой «Вернуть раздел» в конце протокола.</p>
          <div class="row apart"><button type="button" class="ghost" id="prevno">Отмена</button>
            <button type="button" class="danger" id="prevyes">Убрать раздел</button></div>
          <div class="msg" id="prevmsg" role="alert"></div></div>`
      : `<div class="prevbar"><button type="button" class="ghost copybtn dangerlink" id="prevdrop"
          title="Убрать раздел только из протокола этой записи">Убрать этот раздел</button></div>`);
  } else if (p.removed) {
    const why = p.series ? "" : viewFile.includes("/") ? " Вернуть его нельзя: папка записи не отмечена как серия совещаний."
      : " Вернуть его нельзя: запись лежит в корне, а там поручения прошлого совещания не собираются.";
    doc.insertAdjacentHTML("beforeend", `<div class="prevbar gone">
      <span>Раздел «${esc(p.title)}» убран из этого протокола и при пересборке не добавляется.${why}</span>
      ${p.series ? `<button type="button" class="ghost copybtn" id="prevback">Вернуть раздел</button>` : ""}
      <div class="msg" id="prevmsg" role="alert"></div></div>`);
  }
}

async function setPrevious(on) {
  const go = document.getElementById(on ? "prevback" : "prevyes"), live = document.getElementById("vlive");
  const was = go.textContent;
  go.disabled = true;
  go.textContent = on ? "Возвращаю…" : "Убираю…";
  try {
    const d = await api("/api/record/previous_tasks", { file: viewFile, on });
    prevAsk = false;
    await refresh();
    fillDoc();
    live.className = "tsaid";
    live.textContent = !on ? `Раздел «${viewData.previous.title}» убран из протокола и карты` + (viewData.previous.series
        ? ". Вернуть — кнопкой «Вернуть раздел» в конце протокола" : "")
      : d.present ? "Раздел возвращён в конец протокола"
      : "Раздел не добавлен: у предыдущей записи папки нет незакрытых поручений";
    // вернуть было нечего — кнопок у раздела не осталось, о том говорит строка состояния в шапке
    if (!on) focusIn("#prevback");
    else if (d.present) focusIn("#prevdrop");
  } catch (e) {
    go.disabled = false;
    go.textContent = was;
    say("prevmsg", "err", e.message);
    go.focus();
  }
}

function toggleNames(open) {
  namesOpen = open;
  if (!open) namesDraft = {};
  const btn = document.getElementById("vnames");
  if (btn) btn.setAttribute("aria-expanded", String(open));
  fillDoc();
  if (open) { document.querySelector(".doc").scrollTop = 0; focusIn(".names input"); }
}

function readPanel() {
  const parts = {};
  document.querySelectorAll(".names input[data-part]").forEach(i => {
    (parts[i.dataset.part] = parts[i.dataset.part] || {})[i.dataset.label] = i.value.trim();
  });
  return parts;
}

// перенос только в пустые поля: вписанное вручную в этой части не трогаем
function carryNames(index) {
  const from = {};
  document.querySelectorAll(`.names input[data-part="${index - 1}"]`).forEach(i => {
    if (i.value.trim()) from[i.dataset.label] = i.value.trim();
  });
  document.querySelectorAll(`.names input[data-part="${index}"]`).forEach(i => {
    if (!i.value.trim() && from[i.dataset.label]) i.value = from[i.dataset.label];
  });
}

async function sendNames(body, msg, draft) {
  try {
    const d = await api("/api/admin/names", { file: viewFile, ...body });
    // текст с подписями собирает сервер — после правки просто берём его заново
    if (viewKind === "summary") Object.assign(viewData, { names_newer: d.names_newer, rebuilding: d.rebuilding });
    else await refresh();
    return d;
  } catch (e) {
    say(msg, "err", e.message);
    return null;
  }
}

// подписанные ФИО, которых не было в словаре папки, сервер добавляет туда сам — сказать об этом
const dictAdded = d => d.dictionary_added && d.dictionary_added.length
  ? ` В словарь папки добавлено: ${d.dictionary_added.join(", ")} — должность допишите кнопкой «Словарь».` : "";

async function refresh() {
  const d = await api(`/api/result?file=${encodeURIComponent(viewFile)}&kind=${viewKind}`);
  viewData = d;
}

async function saveNames() {
  const parts = readPanel();
  document.getElementById("nsave").disabled = true;
  say("nmsg", "", "Сохраняю…");
  const again = canRebuild();
  const d = await sendNames({ parts, rebuild: again }, "nmsg", () => { namesDraft = parts; });
  if (!d) { const b = document.getElementById("nsave"); if (b) b.disabled = false; return; }
  namesDraft = {};
  fillDoc();
  say("nmsg", "ok", (again ? "Сохранено. Протокол пересобирается — обновится через несколько минут."
    : "Подписи сохранены. Протокол не пересобирался.") + dictAdded(d));
}

async function saveLine(reset) {
  const input = document.querySelector(".lned input[list]");
  const name = reset ? "" : input.value.trim();
  if (!reset && !name) { say("lnmsg", "err", "Впишите ФИО или нажмите «Отмена»"); input.focus(); return; }
  const key = lineEdit;
  if (await sendNames({ lines: { [key]: name } }, "lnmsg")) { lineEdit = null; fillDoc(); }
}

async function rebuild() {
  if (await sendNames({ rebuild: true }, "rbmsg")) fillDoc();
}

/* ---------- словарь папки и записи ----------
   Кто бывает на совещаниях и какие здесь сокращения: по списку сервис сводит имена из записи
   к ФИО и пишет сокращения правильно. Окно одно на три случая: словарь папки, словарь открытой
   записи и словарь записи, которая ещё загружается (он запоминается на странице и уходит на
   сервер, когда запись загрузится). Правит хозяин папки; остальным — тот же список без полей.
   Ввод — по строке на человека или термин: список можно вставить из приглашения на встречу
   или из таблицы, а разбирает строки сервер и возвращает их уже в одном виде. */
let dictState = null;       // {folder | file, data, busy, back, onDraft, was}

const personLine = p => p.name + (p.aka.length ? ` (${p.aka.join(", ")})` : "") + (p.role ? ` — ${p.role}` : "");
const termLine = t => t.term + (t.meaning ? ` — ${t.meaning}` : "");
const DICT_BY = "Словарь правит владелец папки или её администратор";

function dictCount(n, forms) {
  const a = n % 100, b = n % 10;
  return `${n} ${b === 1 && a !== 11 ? forms[0] : b >= 2 && b <= 4 && (a < 12 || a > 14) ? forms[1] : forms[2]}`;
}
const dictSize = (people, terms) => [
  people ? dictCount(people, ["участник", "участника", "участников"]) : "",
  terms ? dictCount(terms, ["сокращение", "сокращения", "сокращений"]) : ""].filter(Boolean).join(", ");

function dictBox() {
  let box = document.getElementById("dict");
  if (box) return box;
  box = document.createElement("div");
  box.id = "dict";
  box.hidden = true;
  document.body.appendChild(box);
  let downOnVeil = false;
  box.addEventListener("mousedown", e => { downOnVeil = e.target.id === "dcveil"; });
  box.addEventListener("click", e => {
    if (e.target.closest("#dcclose, #dccancel") || (e.target.id === "dcveil" && downOnVeil)) closeDict();
    if (e.target.closest("#dcrebuild")) saveDict(true);
  });
  box.addEventListener("submit", e => { e.preventDefault(); saveDict(false); });
  // список из приглашения на встречу приходит одной строкой через «;» — раскладываем по строкам сразу
  box.addEventListener("paste", e => {
    const text = (e.clipboardData || window.clipboardData).getData("text");
    if (e.target.id !== "dcpeople" || !/@/.test(text) || !/;/.test(text)) return;
    e.preventDefault();
    const lines = text.split(/;|\n/).map(s => s.trim()).filter(Boolean).join("\n");
    const el = e.target, from = el.selectionStart, to = el.selectionEnd;
    // вставка в конец непустой строки не должна приклеить первого человека к предыдущему
    const glue = from > 0 && el.value[from - 1] !== "\n" ? "\n" : "";
    el.setRangeText(glue + lines + "\n", from, to, "end");
  });
  // Escape закрывает окно словаря, а не запись под ним; Tab ходит по кругу внутри окна
  addEventListener("keydown", e => {
    if (!dictState || box.hidden) return;
    if (e.key === "Escape") { e.stopPropagation(); closeDict(); return; }
    if (e.key !== "Tab") return;
    const stops = [...box.querySelectorAll("button:not(:disabled), textarea, summary")];
    const first = stops[0], last = stops[stops.length - 1];
    if (!box.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }, true);
  return box;
}

function dictTyped() {
  const people = document.getElementById("dcpeople"), terms = document.getElementById("dcterms");
  return people ? { people: people.value, terms: terms.value } : null;
}

function closeDict() {
  if (!dictState || dictState.busy) return;
  const typed = dictTyped(), was = dictState.was;
  if (typed && was && (typed.people !== was.people || typed.terms !== was.terms)
      && !confirm("Изменения в словаре не сохранены. Закрыть без сохранения?")) return;
  const back = dictState.back;
  dictState = null;
  const box = dictBox();
  box.hidden = true;
  box.innerHTML = "";
  if (back && back.isConnected) back.focus();
}

function dictList(people, terms, origin) {
  const from = x => origin && x.path ? ` <span class="dim">· из папки «${esc(x.path)}»</span>` : "";
  return (people.length ? `<h3>Участники</h3><ul class="dictlist">${people.map(p =>
      `<li><b>${esc(p.name)}</b>${p.role ? ` — ${esc(p.role)}` : ""}${
        p.aka.length ? ` <span class="dim">(ещё называют: ${esc(p.aka.join(", "))})</span>` : ""}${from(p)}</li>`).join("")}</ul>` : "") +
    (terms.length ? `<h3>Сокращения и термины</h3><ul class="dictlist">${terms.map(t =>
      `<li><b>${esc(t.term)}</b>${t.meaning ? ` — ${esc(t.meaning)}` : ""}${from(t)}</li>`).join("")}</ul>` : "");
}

function dictInherited(d) {
  const people = d.inherited.people, terms = d.inherited.terms;
  if (!people.length && !terms.length && !d.common) return "";
  const what = [people.length || terms.length ? `словарь папок выше — ${dictSize(people.length, terms.length)}` : "",
                d.common ? "общий словарь сервиса" : ""].filter(Boolean).join("; ");
  return `<details class="plain dictmore"><summary>Ещё действует: ${what}</summary>
    ${dictList(people, terms, true)}
    ${d.common ? `<p class="dim">Общий словарь сервиса действует на все записи; его правит администратор
      в разделе «Настройки».</p>` : ""}
    <p class="dim">При совпадении побеждает строка, которая ближе к записи: ${
      d.scope === "record" ? "словарь записи, затем её папки, затем папок выше" : "словарь этой папки, затем папок выше"}.</p>
  </details>`;
}

const dictChanged = d => d.changed_at ? `<div class="fhint">Последняя правка: ${markDay(d.changed_at)}${
  d.changed_by ? ` — ${esc(d.changed_by)}` : ""}</div>` : "";

function drawDict(note) {
  const s = dictState, d = s.data, rec = !!s.file, draft = !!s.onDraft;
  const name = rec ? s.file.split("/").pop() : s.folder.split("/").pop();
  const box = dictBox();
  const head = `<div class="veil" id="dcveil"><form class="modal" id="dcform" role="dialog" aria-modal="true"
      aria-labelledby="dctitle" novalidate>
    <header><h2 id="dctitle">Словарь ${rec ? "записи" : "папки"} «${esc(name)}»</h2>
      <button type="button" class="ghost icon" id="dcclose" aria-label="Закрыть">${icon("close", 15)}</button></header>
    <div class="body">`;
  const foot = `</div></form></div>`;
  if (s.error) {
    box.innerHTML = head + `<p class="msg err" role="alert">${esc(s.error)}</p>
      <div class="row"><button type="button" class="ghost" id="dccancel">Закрыть</button></div>` + foot;
    box.hidden = false;
    document.getElementById("dccancel").focus();
    return;
  }
  const empty = !d.people.length && !d.terms.length;
  const why = `<div class="note">Кто бывает на ${rec ? "этом совещании" : "совещаниях этой папки"} и какие здесь
    сокращения. По этому списку сервис пишет в протоколе полные ФИО вместо услышанных кое-как имён и сокращения —
    так, как принято у вас. Список — подсказка: кого в записи не слышно, в протокол не попадёт.
    ${rec ? "Словарь записи дополняет словарь её папки." : "Действует на записи этой папки и её подпапок."}</div>`;
  let body;
  if (!d.can_edit) body = why + (empty
      ? `<div class="empty dictempty">${icon("list", 17)}<span>${rec ? "У этой записи своего словаря нет."
          : "У этой папки словаря пока нет."}</span></div>`
      : dictList(d.people, d.terms, false)) +
    dictInherited(d) + dictChanged(d) + `<p class="dim signby">${DICT_BY}.</p>
    <div class="row"><button type="button" class="ghost" id="dccancel">Закрыть</button></div>`;
  else {
    const people = draft ? s.draft.people : d.people.map(personLine).join("\n");
    const terms = draft ? s.draft.terms : d.terms.map(termLine).join("\n");
    s.was = { people, terms };
    const when = draft ? "Запись ещё загружается: словарь сохранится вместе с ней и учтётся уже в первой обработке."
      : rec && !d.processed ? "Запись ещё не обработана: словарь учтётся при обработке."
      : rec ? "Готовый протокол сам не меняется: чтобы он учёл словарь, протокол пересобирают."
      : "Готовые протоколы сами не меняются: словарь учтётся в записях, обработанных или пересобранных после сохранения.";
    body = why + (empty && !people && !terms ? `<p class="dictempty dim">Пока пусто. Впишите людей и сокращения
        или вставьте готовый список — из приглашения на встречу, из письма, из таблицы.</p>` : "") + `
      <label for="dcpeople">Участники</label>
      <div class="fhint" id="dcpeoplehint">По строке на человека: <b>ФИО — должность или роль</b>. Как ещё называют —
        в скобках после ФИО. Должность и скобки необязательны.</div>
      <textarea id="dcpeople" rows="7" spellcheck="false" autocomplete="off" aria-describedby="dcpeoplehint"
        placeholder="Ершова Лидия Марковна (Лида) — главный бухгалтер&#10;Громов Пётр Андреевич — начальник склада">${esc(people)}</textarea>
      <label for="dcterms">Сокращения и термины</label>
      <div class="fhint" id="dctermshint">По строке: <b>как писать — расшифровка</b>. Расшифровка необязательна.</div>
      <textarea id="dcterms" rows="5" spellcheck="false" autocomplete="off" aria-describedby="dctermshint"
        placeholder="КС-2 — акт о приёмке выполненных работ&#10;план-факт">${esc(terms)}</textarea>
      <div class="fhint">${when}${draft ? "" : ` Не больше ${d.limits.people} участников и ${d.limits.terms} сокращений.`}</div>
      ${dictInherited(d)}
      <div class="row">
        <button id="dcsave">${draft ? "Запомнить" : "Сохранить"}</button>
        ${rec && d.can_rebuild ? `<button type="button" class="ghost" id="dcrebuild"
          title="Платный вызов модели; запись заново не распознаётся">Сохранить и пересобрать протокол</button>` : ""}
        <button type="button" class="ghost" id="dccancel">Закрыть</button>
      </div>
      <div class="msg${note ? " " + note.kind : ""}" id="dcmsg" role="status">${note ? esc(note.text) : ""}</div>
      ${rec && d.processed && !d.can_rebuild ? `<div class="fhint">${REBUILD_BY}.</div>` : ""}
      ${dictChanged(d)}`;
  }
  box.innerHTML = head + body + foot;
  box.hidden = false;
  (document.getElementById(note ? "dcsave" : "dcpeople") || document.getElementById("dccancel")).focus();
}

/* target: {folder} — словарь папки; {file} — словарь записи; {file, draft, onDraft} — запись ещё
   загружается: draft — что уже вписано, onDraft получает вписанное вместо сервера. */
async function openDict(target) {
  dictState = { ...target, back: document.activeElement, busy: false };
  if (target.onDraft) {
    dictState.data = { scope: "record", people: [], terms: [], inherited: { people: [], terms: [] }, common: false,
                       can_edit: true };
    return drawDict();
  }
  try {
    const where = target.file ? `file=${encodeURIComponent(target.file)}` : `path=${encodeURIComponent(target.folder)}`;
    const data = await api(`/api/dictionary?${where}`);
    if (!dictState) return;
    dictState.data = data;
  } catch (e) { dictState.error = e.message; }
  drawDict();
}

async function saveDict(rebuild) {
  const s = dictState, typed = dictTyped();
  if (!s || s.busy || !typed) return;
  if (s.onDraft) {
    s.onDraft(typed);
    s.was = typed;
    return closeDict();
  }
  s.busy = true;
  document.querySelectorAll("#dcform .row button").forEach(b => { b.disabled = true; });
  say("dcmsg", "", rebuild ? "Сохраняю и ставлю протокол на пересборку…" : "Сохраняю…");
  try {
    const d = await api("/api/dictionary", { ...(s.file ? { file: s.file } : { path: s.folder }), ...typed, rebuild });
    s.busy = false;
    s.data = d;
    const size = dictSize(d.people.length, d.terms.length);
    drawDict({ kind: "ok", text: (size ? `Сохранено: ${size}.` : "Сохранено: словарь пуст.") + (rebuild
      ? " Протокол пересобирается — обновится через несколько минут."
      : s.file && d.processed ? " Протокол не пересобирался." : "") });
    // открытая запись узнаёт о новом словаре: подсказки ФИО и строка «протокол устарел»
    if (viewFile && viewData && (!s.file || s.file === viewFile)) { await refresh(); fillDoc(); }
  } catch (e) {
    s.busy = false;
    document.querySelectorAll("#dcform .row button").forEach(b => { b.disabled = false; });
    say("dcmsg", "err", e.message);
    const field = document.getElementById(/сокращени/.test(e.message) ? "dcterms" : "dcpeople");
    if (field) field.focus();
  }
}

function closeViewer(silent) {
  viewFile = null; viewText = ""; viewAudio = false;
  viewData = null; lineEdit = null; dueEdit = null; prevAsk = false; namesOpen = false; namesDraft = {};
  if (onViewerChange && !silent) onViewerChange("close");
  document.getElementById("viewer").innerHTML = "";
  if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  lastFocus = null;
}

function initViewer() {
  const open = e => {
    const link = e.target.closest("a.open");
    if (!link) return;
    e.preventDefault();
    openViewer(link.dataset.file, link.dataset.kind || "summary");
  };
  document.addEventListener("click", open);
  document.addEventListener("keydown", e => {
    // Escape при вопросе «убрать раздел?» — это «Отмена», а не закрытие записи
    if (e.key === "Escape" && prevAsk) { prevAsk = false; fillDoc(); focusIn("#prevdrop"); return; }
    // в поле ввода Escape гасит подсказку браузера, а не закрывает окно с правками
    if (e.key === "Escape" && viewFile && !(e.target.matches && e.target.matches("input"))) closeViewer();
    if ((e.key === "Enter" || e.key === " ") && e.target.closest && e.target.closest("a.open")) open(e);
    const mark = e.target.closest && e.target.closest("a.ts");
    if ((e.key === "Enter" || e.key === " ") && mark) { e.preventDefault(); playFrom(+mark.dataset.sec); }
    if (e.key === "Enter" && e.target.closest && e.target.closest(".lned input[list]")) saveLine(false);
    if (viewFile && e.target.id === "duein") {
      if (e.key === "Enter") { e.preventDefault(); if (dueTyped()) saveDue(dueTyped()); }
      if (e.key === "Escape") closeDue();
    }
  });
}

/* Копирование в буфер. Clipboard API работает только в защищённом контексте:
   на localhost он есть, а если открыть сервис по http с другой машины —
   его не будет. Поэтому запасной путь через скрытое поле. */
async function copyText(text) {
  try {
    if (navigator.clipboard && isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* провалимся в запасной путь */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}
