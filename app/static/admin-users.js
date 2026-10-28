/* Администрирование: «Пользователи» — учётные записи, вход через домен, ключи доступа, заявки, почта.
   Сценарии страницы — обычные, не модули: общие переменные и функции у них одни (admin.js). */
/* ---------- пользователи ----------
   Пока их нет, сервис открыт всем. С первым пользователем вход становится
   обязательным: каждый видит свою личную папку и общие, которые ему открыты. */
let people = null, editing = null;      // editing: логин в форме, "" — новый, null — форма закрыта
let claims = [], mailState = null;      // заявки, ждущие решения, и состояние почты
let claimAct = null;                    // решение по заявке: {id, kind: "approve" | "reject"}
let userAct = null;                     // подтверждение удаления: логин
let bindAct = null;                     // подтверждение привязки к домену: логин
let endAct = null;                      // подтверждение «Завершить входы»: логин
let usersNote = null;                   // что только что сделано: {kind: "ok" | "err", text}
let linkShown = null;                   // ссылка «задать пароль», которую передаёт сам администратор: {login, name, url, hours}
let allKeys = null;                     // все ключи доступа сервиса: {keys, stale_days}
let keyAsk = null, keysNote = null;     // id ключа, про который спросили «отозвать?»; что только что сделано

/* Без почты ссылку «задать пароль» человеку передаёт администратор — и с почтой, если письмо
   не ушло. Сервер отдаёт путь и строку ссылки, адрес берётся тот, по которому открыта эта страница:
   PUBLIC_URL может быть не задан. */
function showLink(login, link) {
  const user = people.users.find(u => u.login === login);
  linkShown = { login, name: user ? user.name : login, url: location.origin + link.path + "#" + link.token, hours: link.hours };
}

async function loadUsers() {
  try {
    people = await api("/api/admin/users", {});
    const d = await api("/api/admin/requests");
    claims = d.requests; mailState = d.mail;
    allKeys = await api("/api/admin/keys");
    dom = await api("/api/admin/domain");
  } catch (e) { return; }
  renderClaims(); renderUsers(); renderAllKeys(); renderMail(); renderDomain();
}

/* ---------- вход через домен ----------
   Настройки — в .env на сервере: здесь они только показаны (пароля служебной учётной записи
   нет). Главное — «Проверить подключение»: пошаговый отчёт, где у каждого отказа написано,
   что делать. Логин и пароль человека для последнего шага никуда не сохраняются. */
let dom = null, domReport = null, domBusy = false;

function renderDomain() {
  const d = dom, p = d.params;
  const yes = (on, good, bad) => on ? good : `<span class="err-text">${bad}</span>`;
  const head = d.on
    ? `<div class="mailbar"><span class="pill"><span class="dot"></span>вход через домен включён</span>
        <span class="grow">${d.secure ? "Люди входят логином и паролем домена на странице входа."
          : `<span class="err-text">Вы открыли сервис по HTTP: по такому адресу вход через домен не предлагается и не принимается.
             Он работает только по https://.</span>`}</span></div>`
    : `<div class="mailbar"><span class="pill down"><span class="dot"></span>вход через домен выключен</span>
        <span class="grow">${esc(d.why_off.charAt(0).toUpperCase() + d.why_off.slice(1))}</span></div>`;
  const warnings = d.warnings.map(w => `<div class="nhint warn lines" role="alert">${icon("alert", 14)}<span><b>Небезопасно.</b> ${esc(w)}</span></div>`).join("");
  const rows = !d.configured ? "" : `<dl class="kv domkv">
      <dt>Контроллеры</dt><dd>${p.urls.map(u => `<code>${esc(u)}</code>`).join("<br>") || "—"}
        <div class="srcnote">по порядку: не ответил первый — сервис идёт к следующему</div></dd>
      <dt>Защита соединения</dt><dd>${p.urls.every(u => u.toLowerCase().startsWith("ldaps://")) ? "LDAPS"
        : p.starttls ? "StartTLS" : `<span class="err-text">нет — пароли идут до контроллера открыто</span>`};
        сертификат контроллера ${yes(p.tls_verify, "проверяется", "НЕ проверяется (LDAP_TLS_VERIFY=0)")}</dd>
      <dt>Корневой сертификат</dt><dd>${p.ca_file ? `<code>${esc(p.ca_file)}</code>`
        : "центры сертификации, которым доверяет система (своего файла нет)"}</dd>
      <dt>База поиска</dt><dd><code>${esc(p.base_dn) || "—"}</code></dd>
      <dt>Служебная учётная запись</dt><dd><code>${esc(p.bind_dn) || "—"}</code>
        <div class="srcnote">пароль ${yes(p.bind_password, "задан в .env; на странице его нет", "не задан (LDAP_BIND_PASSWORD)")}</div></dd>
      <dt>Фильтр людей</dt><dd><code>${esc(p.user_filter)}</code></dd>
      <dt>Группа доступа</dt><dd>${p.access_group ? `<code>${esc(p.access_group)}</code>`
        : "не задана — войти может любой действующий пользователь домена из базы поиска"}</dd>
      <dt>Группа администраторов</dt><dd>${p.admin_group ? `<code>${esc(p.admin_group)}</code>`
        : "не задана — роль администратора сервиса ставится вручную, в списке пользователей"}</dd>
      <dt>Перепроверка</dt><dd>вошедшего сервис сверяет с доменом раз в ${d.recheck_min} мин; домен не отвечает —
        верит последней сверке не дольше ${d.grace_hours} ч</dd>
      <dt>Защита от перебора</dt><dd>не больше ${d.fails.limit} неверных паролей на логин за ${d.fails.minutes} мин —
        чтобы домен не заблокировал человеку учётную запись</dd>
    </dl>`;
  const mark = { true: ["good", "ok", "готово"], false: ["bad", "alert", "отказ"], null: ["soso", "info", "внимание"] };
  const report = !domReport ? "" : `<ol class="steps" aria-label="Отчёт проверки подключения">` + domReport.steps.map(s => {
      const [cls, ic, word] = mark[String(s.ok)];
      return `<li><span class="${cls}">${icon(ic, 17)}</span><div class="body"><b>${esc(s.title)}</b>
        <span class="verdict ${cls}">${word}</span><div>${esc(s.text)}</div>
        ${s.hint ? `<div class="todo"><b>Что делать:</b> ${esc(s.hint)}</div>` : ""}</div></li>`;
    }).join("") + `</ol>`;
  const check = !d.configured ? "" : `
    <form id="domform" novalidate>
      <div class="domcheck">
        <div>${formField("domlogin", "Проверить вход пользователя — логин",
          `<input type="text" id="domlogin" autocomplete="off" autocapitalize="none" spellcheck="false" maxlength="256"
                  aria-describedby="domlogin-hint domlogin-err">`, "Необязательно")}</div>
        <div>${formField("dompass", "Его пароль",
          `<input type="password" id="dompass" autocomplete="off" aria-describedby="dompass-hint dompass-err">`,
          "Уходит в домен на проверку и нигде не сохраняется")}</div>
      </div>
      <div class="row"><button id="domgo" ${domBusy ? "disabled" : ""}>${domBusy ? "Проверяю…" : "Проверить подключение"}</button></div>
      <div class="msg ${domReport ? (domReport.ok ? "ok" : "err") : ""}" id="dommsg" role="status">${!domReport ? ""
        : domReport.ok ? "Проверка пройдена." : "Есть отказы — они отмечены ниже, у каждого написано, что делать."}</div>
    </form>${report}`;
  document.getElementById("domain").innerHTML = head + warnings + rows + check + `
    <div class="note" style="margin-top:12px">${d.configured
      ? `Параметры задаются в файле <code>.env</code> на сервере (строки LDAP_…), после правки контейнер пересоздают.`
      : `Чтобы люди входили логином и паролем домена (Active Directory), заполните в <code>.env</code> строки LDAP_… и пересоздайте контейнер.`}
      Нужен HTTPS. Как подготовить домен и что вписать — инструкция по установке
      (install/README.md), раздел «Вход через домен». Локальные учётные записи и admin из .env входят всегда —
      это запасной вход, если домен недоступен.</div>`;
}

document.getElementById("domain").addEventListener("submit", async e => {
  e.preventDefault();
  if (domBusy) return;
  const login = document.getElementById("domlogin").value.trim(), password = document.getElementById("dompass").value;
  fieldError("dompass", "");
  if (login && !password) { fieldError("dompass", "Введите пароль этого пользователя — или очистите логин"); document.getElementById("dompass").focus(); return; }
  domBusy = true; domReport = null;
  renderDomain();
  document.getElementById("domlogin").value = login;
  try {
    domReport = await api("/api/admin/domain/check", { login, password });
  } catch (err) {
    domReport = { ok: false, steps: [{ title: "Проверка не выполнена", ok: false, text: err.message, hint: "" }] };
  }
  domBusy = false;
  renderDomain();
  document.getElementById("domlogin").value = login;          // пароль в поле не возвращаем
  document.querySelector("#domain .steps").scrollIntoView({ block: "nearest" });
});

/* ---------- ключи доступа всех пользователей ----------
   Ключи человек выпускает себе сам (меню учётной записи); здесь администратор видит их все —
   чей, какого вида, когда создан, когда им пользовались, когда истечёт — и может отозвать любой.
   Значений ключей нет: сервис их не хранит. Истёкшие и давно не использованные помечены словом. */
function renderAllKeys() {
  const day = sec => sec ? new Date(sec * 1000).toLocaleDateString("ru-RU") : "";
  const marks = {
    expired: k => `<span class="kmark expired">${icon("alert", 14)}срок вышел ${day(k.expires)}</span>`,
    stale: () => `<span class="kmark stale">${icon("alert", 14)}не использовался больше ${allKeys.stale_days} дней</span>`,
  };
  const list = allKeys.keys;
  document.getElementById("allkeys").innerHTML =
    (keysNote ? `<div class="msg ${keysNote.kind}" role="status" style="margin:0 0 12px">${esc(keysNote.text)}</div>` : "") +
    (!list.length ? `<div class="empty">${icon("idle", 17)}ключей пока нет — их выпускают в меню учётной записи, «Ключи доступа»</div>`
    : `<div class="scroll"><table class="allkeys"><thead><tr><th>чей</th><th>ключ</th><th>создан</th>
        <th>использован</th><th>истекает</th><th>действия</th></tr></thead><tbody>` +
      list.map(k => `<tr class="${k.mark === "expired" ? "dead" : ""}${keyAsk === k.id ? " picked" : ""}">
        <td>${esc(k.owner_name)}<div class="dim mono" style="font-size:12px">${esc(k.owner)}</div></td>
        <td><b>${esc(k.name)}</b>${k.kind === "manage" ? `<span class="tag warn">с управлением</span>` : ""}
          <div class="dim mono" style="font-size:12px">${esc(k.prefix)}…</div>
          ${k.mark ? `<div>${marks[k.mark](k)}</div>` : ""}</td>
        <td class="num" data-l="создан">${day(k.created)}</td>
        <td class="num" data-l="использован">${k.used ? day(k.used) : `<span class="dim">ни разу</span>`}</td>
        <td class="num" data-l="истекает">${k.expires ? day(k.expires) : `<span class="dim">без срока</span>`}</td>
        <td><div class="rowacts">${keyAsk === k.id
          ? `<span>Отозвать? Клиент с этим ключом потеряет доступ.</span>
             <button type="button" class="ghost" data-keykeep>Оставить</button>
             <button type="button" class="danger" data-keyrevoke="${esc(k.id)}">Отозвать</button>`
          : `<button type="button" class="ghost dangerlink" data-keyask="${esc(k.id)}"
               aria-label="Отозвать ключ «${esc(k.name)}» пользователя ${esc(k.owner)}">Отозвать</button>`}</div></td>
      </tr>`).join("") + `</tbody></table></div>`) +
    `<div class="note" style="margin-top:12px">Ключ даёт ИИ-клиенту права своего владельца. Ключ «с управлением»
      есть только у администраторов сервиса и живёт не дольше 90 дней: им можно заводить пользователей и раздавать
      права. Выпустить ключ можно только себе — в меню учётной записи. Отзыв записывается в журнал действий.</div>`;
}

document.getElementById("allkeys").addEventListener("click", async e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.keyask) {
    keyAsk = b.dataset.keyask; keysNote = null;
    renderAllKeys();
    document.querySelector("#allkeys [data-keykeep]").focus();      // случайный Enter ничего не отзовёт
  } else if ("keykeep" in b.dataset) {
    const id = keyAsk;
    keyAsk = null;
    renderAllKeys();
    document.querySelector(`#allkeys [data-keyask="${id}"]`).focus();
  } else if (b.dataset.keyrevoke) {
    b.disabled = true;
    const gone = allKeys.keys.find(k => k.id === b.dataset.keyrevoke);
    try {
      allKeys.keys = (await api("/api/admin/keys/revoke", { id: b.dataset.keyrevoke })).keys;
      keysNote = { kind: "ok", text: `Ключ «${gone.name}» пользователя ${gone.owner} отозван.` };
    } catch (err) { keysNote = { kind: "err", text: "Ключ не отозван: " + err.message }; }
    keyAsk = null;
    renderAllKeys();
  }
});

/* Счётчик заявок в меню — из того же ответа, что и список: решили заявку, и он сразу меньше. */
function setClaims(list) {
  claims = list;
  me.requests = list.length;
  renderNav();
}

const when = at => `${at.slice(8, 10)}.${at.slice(5, 7)}.${at.slice(0, 4)} ${at.slice(11, 16)}`;

/* ---------- заявки на доступ ----------
   Человек оставил заявку на странице входа. «Открыть доступ» заводит пользователя с личной
   папкой и без пароля; логин и ФИО перед этим можно поправить. Ссылка «задать пароль» уходит
   ему письмом, а без почты (в сервисе или в заявке) показывается здесь — передать самому.
   «Отклонить» — без письма. Оба решения подтверждаются тут же, в карточке заявки. */
function renderClaims() {
  const card = document.getElementById("claimscard");
  const mailOn = mailState && mailState.on;
  card.hidden = false;
  document.getElementById("claims").innerHTML = !claims.length
    ? `<div class="empty">${icon("idle", 17)}новых заявок нет — они появляются, когда человек нажимает «Запросить доступ» на странице входа</div>`
    : claims.map(c => {
        const act = claimAct && claimAct.id === c.id ? claimAct.kind : "";
        const id = esc(c.id), byLetter = mailOn && c.email;
        return `<div class="claim${act === "reject" ? " dangerbox" : ""}" data-claim="${id}">
          <div class="who">${esc(c.name)}</div>
          <div class="facts"><span>почта: ${c.email ? esc(c.email) : "не указана"}</span><span>логин: <b class="mono">${esc(c.login)}</b></span>
            <span>подана ${when(c.at)}</span></div>
          ${c.comment ? `<div class="why">${esc(c.comment)}</div>` : ""}
          ${act === "approve" ? `<form class="approve" data-id="${id}" novalidate>
              <div class="fields">
                <div>${formField("clogin", "Логин",
                  `<input type="text" id="clogin" autocomplete="off" autocapitalize="none" spellcheck="false" maxlength="32"
                          value="${esc(c.login)}" aria-describedby="clogin-err" required>`, "", true)}</div>
                <div>${formField("cname", "Фамилия, имя, отчество",
                  `<input type="text" id="cname" autocomplete="off" maxlength="120" value="${esc(c.name)}"
                          aria-describedby="cname-err" required>`, "", true)}</div>
              </div>
              <label class="check"><input type="checkbox" id="chomeup" aria-describedby="chomeup-hint">
                <b>Разрешить загрузку в личную папку</b></label>
              <div class="fhint" id="chomeup-hint">Обработка каждой записи стоит денег у поставщика. Без галочки человек только
                читает то, что ему откроют; разрешить загрузку можно и позже — кнопкой «Изменить» в строке пользователя.</div>
              <div class="note" style="margin-top:12px">Будет заведён пользователь с личной папкой, без пароля. ${byLetter
                ? `На ${esc(c.email)} уйдёт письмо с логином и одноразовой ссылкой «задать пароль» на 48 часов.`
                : `${mailOn ? "В заявке нет почты" : "Сервис писем не шлёт"}, поэтому одноразовую ссылку «задать пароль»
                   на 48 часов вы получите здесь и передадите человеку сами.`} Права на общие папки
                раздаются потом, в разделе «Папки и доступ»: чтение, загрузка и администрирование — по отдельности.</div>
              <div class="row"><button id="cgo" data-label="${byLetter ? "Открыть доступ и отправить письмо" : "Открыть доступ и показать ссылку"}">${
                byLetter ? "Открыть доступ и отправить письмо" : "Открыть доступ и показать ссылку"}</button>
                <button type="button" class="ghost" data-claimact="">Отмена</button></div>
              <div class="msg" id="cmsg" role="alert"></div></form>`
          : act === "reject" ? `<div role="alertdialog" aria-label="Отклонить заявку">
              <div class="note" style="margin-top:12px">Отклонить заявку? Человеку об этом никто не сообщит; заявка исчезнет
                из списка, а логин и почту можно будет указать в новой заявке.</div>
              <div class="row"><button type="button" class="danger" id="creject" data-id="${id}">Отклонить заявку</button>
                <button type="button" class="ghost" data-claimact="">Отмена</button></div>
              <div class="msg" id="cmsg" role="alert"></div></div>`
          : `<div class="row"><button type="button" data-claimact="approve">Открыть доступ</button>
              <button type="button" class="ghost dangerlink" data-claimact="reject">Отклонить</button></div>`}
        </div>`;
      }).join("");
  const focus = document.getElementById("clogin") || document.getElementById("creject");
  if (focus && claimAct && claimAct.fresh) { focus.focus(); claimAct.fresh = false; }
}

document.getElementById("claims").addEventListener("click", async e => {
  const b = e.target.closest("button");
  if (!b) return;
  if ("claimact" in b.dataset) {
    const id = b.closest("[data-claim]").dataset.claim;
    claimAct = b.dataset.claimact ? { id, kind: b.dataset.claimact, fresh: true } : null;
    renderClaims();
  } else if (b.id === "creject") {
    b.disabled = true;
    try {
      const done = await api("/api/admin/requests/reject", { id: b.dataset.id });
      claimAct = null;
      usersNote = { kind: "ok", text: "Заявка отклонена. Письмо человеку не отправлялось." };
      setClaims(done.requests); renderClaims(); renderUsers();
    } catch (err) { b.disabled = false; say("cmsg", "err", err.message); }
  }
});

document.getElementById("claims").addEventListener("submit", async e => {
  e.preventDefault();
  const login = document.getElementById("clogin").value.trim(), name = document.getElementById("cname").value.trim();
  const wrong = [["clogin", login ? "" : "Укажите логин"], ["cname", name ? "" : "Укажите ФИО"]];
  wrong.forEach(([id, text]) => fieldError(id, text));
  const first = wrong.find(([, text]) => text);
  if (first) { document.getElementById(first[0]).focus(); return; }
  const button = document.getElementById("cgo");
  button.disabled = true;
  button.textContent = "Открываю доступ…";
  try {
    const done = await api("/api/admin/requests/approve", { id: e.target.dataset.id, login, name,
                                                             home_upload: document.getElementById("chomeup").checked });
    claimAct = null;
    people.users = done.users;
    // пользователь заведён в любом случае; не ушло письмо — говорим об этом и показываем ссылку, как без почты
    linkShown = null;
    if (done.link) showLink(done.login, done.link);
    usersNote = done.mail_error
      ? { kind: "err", text: `Пользователь ${done.login} заведён, но письмо на ${done.email} не ушло: ${done.mail_error}. ` +
          (done.link ? `Передайте ему ссылку ниже сами.` : `Выпустите ссылку на странице «Пользователи» и передайте её сами.`) }
      : done.link ? { kind: "ok", text: `Доступ открыт: ${done.login}. Пароля у него пока нет — передайте ему ссылку ниже.` }
      : { kind: "ok", text: `Доступ открыт: ${done.login}. Письмо со ссылкой «задать пароль» отправлено на ${done.email}.` };
    setClaims(done.requests); renderClaims(); renderUsers();
    if (done.link) document.getElementById("linkurl").focus();
    loadSidebar();                              // в меню появилась личная папка
  } catch (err) {
    button.disabled = false;
    button.textContent = button.dataset.label;
    // отказ про логин — под полем логина: его и нужно поправить
    if (/логин/i.test(err.message) && fieldError("clogin", err.message)) document.getElementById("clogin").focus();
    else say("cmsg", "err", err.message);
  }
});

// Права одной строкой на папку: «Проекты» или «Проекты · только эта папка».
function grants(map) {
  const rows = Object.entries(map).sort().map(([f, scope]) =>
    esc(f || "все папки") + (scope === "tree" ? "" : ` <span class="dim">· только эта папка</span>`));
  return rows.length ? rows.join("<br>") : `<span class="dim">—</span>`;
}

// «Завершить входы»: все браузеры человека выходят, ключи и пароль остаются — с подтверждением ниже таблицы
const endButton = u => `<button class="ghost dangerlink" data-end="${esc(u.login)}"
  aria-label="Завершить все входы пользователя ${esc(u.login)}"
  title="Выкинуть со всех браузеров и устройств: войти заново он сможет своим паролем; ключи доступа не отзываются">Завершить входы</button>`;

function renderUsers() {
  const box = document.getElementById("users");
  const list = people.users;
  const edited = editing ? list.find(u => u.login === editing) : null;
  const mailOn = mailState && mailState.on;
  const foreign = u => u.source === "domain";
  const seenAt = sec => new Date(sec * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
  const byGroup = dom && dom.params.admin_group;        // роль доменным пользователям задаёт группа домена
  // у учётной записи домена ФИО и почта приходят из домена, пароля в сервисе нет — править остаётся роль
  // (если её не задаёт группа домена) и загрузку в личную папку
  const form = editing === null ? "" : edited && foreign(edited) ? `
    <form class="userform" id="uform" novalidate>
      <input type="hidden" id="ulogin" value="${esc(editing)}"><input type="hidden" id="uname" value="${esc(edited.name)}">
      <input type="hidden" id="uemail" value=""><input type="hidden" id="upass" value="">
      <div class="note" style="margin-top:0"><b>${esc(edited.name)}</b> (<span class="mono">${esc(edited.login)}</span>) — учётная запись домена.
        ФИО и почта берутся из домена при каждом входе; пароль проверяется и меняется в домене, в сервисе его нет.</div>
      <label class="check"><input type="checkbox" id="uadmin" ${edited.admin ? "checked" : ""} ${byGroup ? "disabled" : ""}>
        <span><b>Администратор сервиса</b> — видит все записи, заводит пользователей, раздаёт доступ${
          byGroup ? " (роль задаёт группа домена)" : ""}</span></label>
      <label class="check"><input type="checkbox" id="uhomeup" aria-describedby="uhomeup-hint" ${edited && edited.home_upload ? "checked" : ""}>
        <span><b>Загрузка в личную папку</b> — человек сам кладёт записи в «Пользовательские/логин»</span></label>
      <div class="fhint" id="uhomeup-hint">Обработка каждой записи стоит денег у поставщика. Без этой галочки личная папка у человека
        есть, но загружать в неё он не может. На общие папки загрузка выдаётся отдельно — в разделе «Папки и доступ».</div>
      <div class="row"><button>Сохранить</button><button type="button" class="ghost" id="ucancel">Отмена</button></div>
      <div class="msg" id="umsg" role="alert"></div>
    </form>` : `
    <form class="userform" id="uform" novalidate>
      ${formField("ulogin", "Логин — латиницей, без пробелов",
        `<input type="text" id="ulogin" autocomplete="off" autocapitalize="none" spellcheck="false" maxlength="32"
                value="${esc(editing)}" ${editing ? "readonly" : ""} aria-describedby="ulogin-err" required>`, "", !editing)}
      <label for="uname">Имя, как показывать на странице</label>
      <input type="text" id="uname" autocomplete="off" maxlength="120" value="${esc(edited ? edited.name : "")}">
      ${formField("uemail", "Почта",
        `<input type="email" id="uemail" autocomplete="off" spellcheck="false" maxlength="254"
                value="${esc(edited ? edited.email : "")}" aria-describedby="uemail-hint uemail-err">`,
        mailOn ? "Необязательно. На неё уходит ссылка «задать пароль» — и когда человек сам нажал «Забыл пароль»"
               : "Необязательно; сейчас сервис писем не шлёт")}
      ${formField("upass", editing ? "Новый пароль — пусто, если менять не нужно" : "Пароль, не короче 8 знаков",
        `<div class="row" style="margin-top:0">
          <input type="text" id="upass" autocomplete="off" spellcheck="false" style="flex:1 1 220px"
                 aria-describedby="upass-err" ${editing ? "" : "required"}>
          <button type="button" class="ghost" id="ugen">Сгенерировать</button>
          <button type="button" class="ghost" id="ucopy">Копировать</button>
        </div>`, "", !editing)}
      <div class="note" style="margin-top:8px">Пароль виден, чтобы его можно было передать человеку: после сохранения
        сервис его больше не покажет. Свой пароль человек потом меняет сам.</div>
      <label class="check"><input type="checkbox" id="uadmin" ${edited && edited.admin ? "checked" : ""}>
        <span><b>Администратор сервиса</b> — видит все записи, заводит пользователей, раздаёт доступ</span></label>
      <label class="check"><input type="checkbox" id="uhomeup" aria-describedby="uhomeup-hint" ${edited && edited.home_upload ? "checked" : ""}>
        <span><b>Загрузка в личную папку</b> — человек сам кладёт записи в «Пользовательские/логин»</span></label>
      <div class="fhint" id="uhomeup-hint">Обработка каждой записи стоит денег у поставщика. Без этой галочки личная папка у человека
        есть, но загружать в неё он не может. На общие папки загрузка выдаётся отдельно — в разделе «Папки и доступ».</div>
      <div class="note" style="margin-top:12px">Личная папка «Пользовательские/логин» появляется у каждого сама.
        Права на общие папки раздаются в разделе «Папки и доступ»: у каждой папки своя таблица —
        чтение, загрузка и администрирование.</div>
      <div class="row"><button>Сохранить</button><button type="button" class="ghost" id="ucancel">Отмена</button>
        ${editing && dom && dom.on ? `<button type="button" class="ghost" data-bind="${esc(editing)}"
          title="Сделать учётную запись доменной: вход паролем домена, пароль сервиса стирается, права остаются">Привязать к домену…</button>` : ""}</div>
      <div class="msg" id="umsg" role="alert"></div>
    </form>`;
  const doomed = userAct ? list.find(u => u.login === userAct) : null;
  const confirmBox = !doomed ? "" : `<div class="up dangerbox" style="margin-top:14px" role="alertdialog" aria-labelledby="udeltitle">
      <div class="uphead"><span class="upname" id="udeltitle">${foreign(doomed) ? "Убрать из сервиса" : "Удалить"} пользователя ${esc(doomed.name)} (${esc(doomed.login)})?</span></div>
      <ul class="plain">${foreign(doomed)
        ? `<li>выданные ему права на папки пропадут, его ключи доступа перестанут действовать;</li>
           <li><b>это учётная запись домена: при следующем входе она заведётся снова</b> — без прежних прав, —
             если человек действует в домене${dom && dom.params.access_group ? " и остался в группе доступа" : ""}.
             Чтобы закрыть ему вход, ${dom && dom.params.access_group ? "уберите его из группы доступа в домене" : "отключите его в домене или задайте группу доступа"};</li>`
        : `<li>войти в сервис он больше не сможет, выданные ему права пропадут, его ключи доступа перестанут действовать;</li>`}
        <li>его личная папка и записи в ней останутся на месте.</li></ul>
      <div class="row"><button type="button" class="danger" id="udelete">${foreign(doomed) ? "Убрать из сервиса" : "Удалить пользователя"}</button>
        <button type="button" class="ghost" id="udelcancel">Отмена</button></div>
      <div class="msg" id="udelmsg" role="alert"></div></div>`;
  const tied = bindAct ? list.find(u => u.login === bindAct) : null;
  const bindBox = !tied ? "" : `<div class="up" style="margin-top:14px" role="alertdialog" aria-labelledby="ubindtitle">
      <div class="uphead"><span class="upname" id="ubindtitle">Привязать ${esc(tied.name)} (${esc(tied.login)}) к домену?</span></div>
      <ul class="plain"><li>входить он будет логином <b class="mono">${esc(tied.login)}</b> и паролем домена; пароль, заданный в сервисе,
          стирается, и прежние входы перестают действовать;</li>
        <li>ФИО и почта заменятся теми, что записаны в домене; права на папки, личная папка и ключи доступа останутся;</li>
        <li>привязка пройдёт, только если в домене есть действующий пользователь с таким логином${dom.params.access_group ? " из группы доступа" : ""};</li>
        <li>обратно в локальную учётную запись она не превращается: для этого запись убирают и заводят заново.</li></ul>
      <div class="row"><button type="button" id="ubind">Привязать к домену</button>
        <button type="button" class="ghost" id="ubindcancel">Отмена</button></div>
      <div class="msg" id="ubindmsg" role="alert"></div></div>`;
  const ended = endAct ? list.find(u => u.login === endAct) : null;
  const endBox = !ended ? "" : `<div class="up dangerbox" style="margin-top:14px" role="alertdialog" aria-labelledby="uendtitle">
      <div class="uphead"><span class="upname" id="uendtitle">Завершить все входы ${esc(ended.name)} (${esc(ended.login)})?</span></div>
      <ul class="plain"><li>на всех его браузерах и устройствах вход перестанет действовать — сразу;</li>
        <li>войти заново он сможет своим ${foreign(ended) ? "паролем домена" : "паролем"}: пароль, права и личная папка не меняются;</li>
        <li>его ключи доступа продолжат работать — отозвать их можно ниже, в «Ключах доступа».</li></ul>
      <div class="row"><button type="button" class="danger" id="uend">Завершить входы</button>
        <button type="button" class="ghost" id="uendcancel">Отмена</button></div>
      <div class="msg" id="uendmsg" role="alert"></div></div>`;
  const linkBox = !linkShown ? "" : `<div class="linkbox" role="group" aria-labelledby="linktitle">
      <div class="linktitle" id="linktitle">Ссылка «задать пароль» для ${esc(linkShown.name)} (логин <b class="mono">${esc(linkShown.login)}</b>)</div>
      <div class="linkrow">
        <input type="text" id="linkurl" readonly spellcheck="false" value="${esc(linkShown.url)}" aria-label="Одноразовая ссылка «задать пароль»"
               aria-describedby="linkhint">
        <button type="button" id="linkcopy">${icon("clip", 14)}Копировать</button>
        <button type="button" class="ghost" id="linkhide">Скрыть</button>
      </div>
      <div class="fhint" id="linkhint">Передайте человеку любым способом вместе с логином: по ссылке он сам задаст пароль.
        Ссылка действует ${linkShown.hours} часов и срабатывает один раз. Сервис её не хранит и больше не покажет —
        если она потеряется, выпустите новую кнопкой «Показать ссылку» в строке пользователя (прежняя перестанет действовать).</div>
      <div class="msg" id="linkmsg" role="status"></div></div>`;
  box.innerHTML =
    (usersNote ? `<div class="msg ${usersNote.kind}" role="status" style="margin:0 0 12px">${esc(usersNote.text)}</div>` : "") +
    linkBox +
    (list.length ? "" : `<div class="note">Пользователей пока нет: войти в сервис можете только вы, под логином <b>admin</b>.</div>`) +
    (list.length ? `<div class="scroll"><table><thead><tr>
        <th>логин</th><th>имя</th><th>почта</th><th>источник</th><th>личная папка</th><th>чтение</th><th>загрузка</th><th>администрирование</th><th>действия</th></tr></thead><tbody>
        <tr><td class="mono">admin</td><td>Администратор</td><td><span class="dim">—</span></td>
          <td>встроенный</td><td><span class="dim">—</span></td>
          <td colspan="3">всё<div class="dim" style="font-size:12px">администратор сервиса, пароль в .env</div></td><td></td></tr>` +
      list.map(u => `<tr${[userAct, editing, bindAct, endAct].includes(u.login) ? ' class="picked"' : ""}>
        <td class="mono">${esc(u.login)}${u.has_password || foreign(u) ? "" :
          `<div><span class="tag warn" title="Человек ещё не задал пароль по одноразовой ссылке — войти он не может">пароль не задан</span></div>`}</td>
        <td>${esc(u.name)}</td><td>${u.email ? esc(u.email) : `<span class="dim">—</span>`}</td>
        <td>${foreign(u) ? `домен<div class="srcnote">${u.checked ? `подтверждён ${seenAt(u.checked)}`
              : `<span class="err-text">домен отказал — вход закрыт</span>`}</div>` : "локальный"}</td>
        <td>${esc(u.home)}<div class="dim" style="font-size:12px">${
          u.admin || u.home_upload ? "загрузка разрешена" : "без загрузки"}</div></td>
        ${u.admin ? `<td colspan="3">всё<div class="dim" style="font-size:12px">администратор сервиса${
              foreign(u) && byGroup ? " — по группе домена" : ""}</div></td>`
          : `<td>${grants(u.read)}</td><td>${grants(u.upload)}</td><td>${grants(u.manage)}</td>`}
        <td><div class="rowacts">${foreign(u)
          // пароля у доменной записи нет: ни задать, ни выслать ссылку; роль при заданной группе правит домен,
          // но загрузку в личную папку и ей выдаёт администратор сервиса
          ? `<button class="ghost" data-edit="${esc(u.login)}">Изменить</button>
             <button class="ghost dangerlink" data-del="${esc(u.login)}"
               aria-label="Убрать из сервиса пользователя ${esc(u.login)}" title="Убрать из сервиса. Учётная запись домена: при следующем входе заведётся снова, если человек остался в домене и в группе доступа">Убрать</button>
             ${endButton(u)}`
          : `<button class="ghost" data-edit="${esc(u.login)}">Изменить</button>
            ${mailOn && u.email ? `<button class="ghost" data-link="${esc(u.login)}"
              title="Отправить на ${esc(u.email)} новую ссылку «задать пароль»; прежняя перестанет действовать">Выслать ссылку</button>`
            : `<button class="ghost" data-link="${esc(u.login)}"
              title="Выпустить новую одноразовую ссылку «задать пароль» и показать её здесь — передать человеку самому; прежняя перестанет действовать">Показать ссылку</button>`}
            <button class="ghost dangerlink" data-del="${esc(u.login)}">Удалить</button>
            ${endButton(u)}`}</div></td>
      </tr>`).join("") + `</tbody></table></div>` : "") +
    (dom && dom.on && list.some(u => !foreign(u)) ? `<div class="note" style="margin-top:12px">Локальная учётная запись сама доменной не становится.
      Пока она есть, человек с тем же логином в домене войти доменным паролем не может: логин занят. «Привязать к домену» (кнопка «Изменить» в строке пользователя) переводит
      запись на вход через домен с сохранением прав.</div>` : "") +
    confirmBox + bindBox + endBox +
    (editing === null ? `<div class="row"><button class="ghost" id="uadd">Добавить пользователя</button></div>` : form);
}

/* ---------- почта ----------
   Настройки — в .env на сервере; здесь видно, включена ли почта, и можно послать пробное
   письмо. Ошибка почтового сервера показывается как есть: по ней и чинят настройки. */
function renderMail() {
  const m = mailState;
  document.getElementById("mail").innerHTML = m.on ? `
    <div class="mailbar"><span class="pill"><span class="dot"></span>почта включена</span>
      <span class="grow">${m.admin_emails.length
        ? `Уведомления о заявках уходят на: <b>${m.admin_emails.map(esc).join(", ")}</b>`
        : `<span class="err-text">ADMIN_EMAIL в .env не задан — письмо о новой заявке никому не уйдёт, её видно только счётчиком в меню</span>`}</span>
      <button type="button" class="ghost" id="mailtest" ${m.probe_to.length ? "" : "disabled"}>Отправить пробное письмо</button></div>
    <div class="fhint">${m.probe_to.length ? `Пробное письмо уйдёт на ${m.probe_to.map(esc).join(", ")}.`
      : "Пробное письмо слать некуда: укажите свою почту в учётной записи или ADMIN_EMAIL в .env."}</div>
    ${m.warnings.map(w => `<div class="nhint warn lines" role="note">${icon("alert", 14)}<span><b>Небезопасно.</b> ${esc(w)}</span></div>`).join("")}
    <div class="msg" id="mailmsg" role="status"></div>
    <pre class="smtperr" id="mailerr" role="alert"></pre>` : `
    <div class="mailbar"><span class="pill down"><span class="dot"></span>почта выключена</span>
      <span class="grow">${esc(m.why_off || "SMTP_HOST в .env не задан")}</span></div>
    <div class="note" style="margin-top:12px">Заявки на доступ принимаются и без почты: они появляются выше, в блоке «Заявки на доступ»,
      и счётчиком в меню. Одноразовую ссылку «задать пароль» сервис тогда показывает вам — передать человеку самому.
      Ссылки «Забыл пароль» на странице входа без почты нет. Чтобы сервис слал письма сам, заполните в <code>.env</code> блок почты
      (SMTP_HOST, SMTP_FROM, PUBLIC_URL, ADMIN_EMAIL) и пересоздайте контейнер — см. инструкцию по установке (install/README.md), «Почта и заявки на доступ».</div>`;
}

document.getElementById("mail").addEventListener("click", async e => {
  const b = e.target.closest("#mailtest");
  if (!b) return;
  b.disabled = true;
  document.getElementById("mailerr").textContent = "";
  say("mailmsg", "", "Отправляю…");
  try {
    const done = await api("/api/admin/mail/test", {});
    say("mailmsg", "ok", `Почтовый сервер принял письмо для ${done.to.join(", ")}. Проверьте ящик, в том числе «Спам».`);
  } catch (err) {
    say("mailmsg", "err", "Письмо не ушло. Ответ почтового сервера:");
    document.getElementById("mailerr").textContent = err.message;
  }
  b.disabled = false;
});

document.getElementById("users").addEventListener("click", async e => {
  const t = e.target.closest("button");
  if (!t) return;
  if (t.id === "uadd") { editing = ""; userAct = endAct = null; usersNote = null; renderUsers(); document.getElementById("ulogin").focus(); }
  else if (t.id === "ucancel") { editing = null; renderUsers(); }
  else if (t.id === "ugen") { document.getElementById("upass").value = newPassword(); fieldError("upass", ""); }
  else if (t.id === "ucopy") { copyText(document.getElementById("upass").value); t.textContent = "Скопировано";
                               setTimeout(() => { t.textContent = "Копировать"; }, 2000); }
  else if (t.dataset.edit) { editing = t.dataset.edit; userAct = endAct = null; usersNote = null; renderUsers(); document.getElementById("uname").focus(); }
  else if (t.dataset.del) { userAct = t.dataset.del; bindAct = endAct = null; editing = null; usersNote = null; renderUsers(); document.getElementById("udelete").focus(); }
  else if (t.id === "udelcancel") { userAct = null; renderUsers(); }
  else if (t.dataset.bind) { bindAct = t.dataset.bind; userAct = endAct = null; editing = null; usersNote = null; renderUsers(); document.getElementById("ubindcancel").focus(); }
  // случайный Enter в подтверждении никого не выкинет: фокус — на «Отмене»
  else if (t.dataset.end) { endAct = t.dataset.end; userAct = bindAct = null; editing = null; usersNote = null; renderUsers(); document.getElementById("uendcancel").focus(); }
  else if (t.id === "uendcancel") { const back = endAct; endAct = null; renderUsers(); document.querySelector(`[data-end="${CSS.escape(back)}"]`)?.focus(); }
  else if (t.id === "uend") {
    t.disabled = true;
    t.textContent = "Завершаю…";
    try {
      const done = await api("/api/admin/users/sessions", { login: endAct });
      endAct = null;
      usersNote = { kind: "ok", text: `Все входы ${done.login} завершены: на страницу он войдёт заново своим паролем. Ключи доступа не отозваны.` };
      renderUsers();
    } catch (err) { t.disabled = false; t.textContent = "Завершить входы"; say("uendmsg", "err", err.message); }
  }
  else if (t.id === "ubindcancel") { bindAct = null; renderUsers(); }
  else if (t.id === "ubind") {
    t.disabled = true;
    t.textContent = "Спрашиваю домен…";
    try {
      const tied = bindAct;
      people.users = (await api("/api/admin/users/domain", { login: tied })).users;
      bindAct = null;
      usersNote = { kind: "ok", text: `Учётная запись ${tied} привязана к домену: дальше вход — логином и паролем домена.` };
      renderUsers();
    } catch (err) { t.disabled = false; t.textContent = "Привязать к домену"; say("ubindmsg", "err", err.message); }
  }
  else if (t.id === "udelete") {
    t.disabled = true;
    try {
      const gone = userAct;
      people.users = (await api("/api/admin/users/delete", { login: gone })).users;
      userAct = null;
      usersNote = { kind: "ok", text: `Пользователь ${gone} удалён. Его личная папка и записи остались на месте.` };
      renderUsers();
      allKeys = await api("/api/admin/keys");       // его ключи погасли вместе с учётной записью
      renderAllKeys();
    } catch (err) { t.disabled = false; say("udelmsg", "err", err.message); }
  }
  else if (t.dataset.link) {
    t.disabled = true;
    linkShown = null;
    try {
      const done = await api("/api/admin/users/link", { login: t.dataset.link });
      if (done.link) showLink(done.login, done.link);
      usersNote = done.mail_error
        ? { kind: "err", text: `Письмо на ${done.email} не ушло: ${done.mail_error}. Новая ссылка для ${done.login} — ниже, ` +
            `передайте её сами. Прежняя ссылка больше не действует.` }
        : { kind: "ok", text: done.link ? `Новая ссылка для ${done.login} — ниже. Прежняя ссылка больше не действует.`
            : `Ссылка «задать пароль» отправлена на ${done.email}. Прежняя ссылка больше не действует.` };
    } catch (err) { usersNote = { kind: "err", text: `${t.dataset.link}: ${err.message}` }; }
    renderUsers();
    if (linkShown) document.getElementById("linkurl").focus();
  }
  else if (t.id === "linkcopy") {
    const ok = await copyText(linkShown.url);
    say("linkmsg", ok ? "ok" : "err", ok ? "Ссылка скопирована." : "Скопировать не вышло — выделите ссылку в поле и скопируйте сами.");
    if (!ok) document.getElementById("linkurl").select();
  }
  else if (t.id === "linkhide") { linkShown = null; usersNote = null; renderUsers(); }
});

document.getElementById("users").addEventListener("submit", async e => {
  e.preventDefault();
  const val = id => document.getElementById(id).value;
  ["ulogin", "uemail", "upass"].forEach(id => fieldError(id, ""));
  try {
    people.users = (await api("/api/admin/users/save",
      { login: val("ulogin"), name: val("uname"), password: val("upass"), email: val("uemail"),
        admin: document.getElementById("uadmin").checked,
        home_upload: document.getElementById("uhomeup").checked })).users;
    usersNote = { kind: "ok", text: `Пользователь ${val("ulogin").trim().toLowerCase()} сохранён.` };
    editing = null;
    loadUsers();                    // список папок тоже мог пополниться
  } catch (err) {
    // отказ сервера — под тем полем, о котором он говорит
    const id = /почт/i.test(err.message) ? "uemail" : /парол/i.test(err.message) ? "upass" : /логин/i.test(err.message) ? "ulogin" : "";
    if (id && fieldError(id, err.message)) document.getElementById(id).focus();
    else say("umsg", "err", err.message);
  }
});
