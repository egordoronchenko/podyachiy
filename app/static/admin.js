let state = null, filesCache = null, busy = false;
// лог и история приходят приростом: держим их у себя и просим только новое
let logLines = [], logSeen = 0, history = [], historyVersion = -1;

initSidebar("admin");
initTheme();
initViewer();

async function tick() {
  try {
    const d = await api(`/api/admin/status?log_from=${logSeen}&history_version=${historyVersion}`);
    // сервис мог перезапуститься — тогда нумерация строк начнётся заново
    // и придёт не тот кусок, которого мы ждали: берём его целиком
    logLines = d.log_start === logSeen ? logLines.concat(d.log) : d.log.slice();
    logLines = logLines.slice(-400);
    logSeen = d.log_total;
    if (d.history !== null) { history = d.history; historyVersion = d.history_version; }
    state = d;
    render();
  } catch (e) {
    const pill = document.getElementById("livepill");
    pill.className = "pill down";
    pill.innerHTML = `<span class="dot"></span>сервис недоступен`;
  }
}

/* База и резервная копия — двумя плашками в шапке «Обработки»: версия схемы и размер базы; когда команда
   копии отработала последний раз. Копии не было — так и сказано словами, со ссылкой на инструкцию. */
function storageChips(s) {
  const size = bytes => bytes >= 2 ** 30 ? `${(bytes / 2 ** 30).toFixed(1).replace(".", ",")} ГБ`
    : bytes >= 2 ** 20 ? `${(bytes / 2 ** 20).toFixed(1).replace(".", ",")} МБ` : `${Math.max(1, Math.round(bytes / 1024))} КБ`;
  const where = { desktop: "папка Docker Desktop", network: "сетевая папка", local: "" }[s.kind];
  const base = `<span class="chip" title="Файл podyachiy.db в папке состояния">база <b>${size(s.bytes)}</b>, схема <b>${s.schema}</b>${
    s.fs ? `, ${esc(s.fs)}${where ? ` — ${where}` : ""}` : ""}</span>`;
  const b = s.backup;
  const copy = b
    ? `<span class="chip${b.skipped ? " alarm" : ""}" title="${esc(b.file)}">${b.skipped ? icon("alert", 14) + " " : ""}копия <b>${esc(when(b.at))}</b>, ${size(b.bytes)}${
        b.inbox ? "" : ", без исходных записей"}${b.skipped ? `, не прочитано файлов: ${b.skipped}` : ""}</span>`
    : `<span class="chip">резервная копия не делалась — <a href="/install-guide">как сделать</a></span>`;
  return base + copy;
}

function render() {
  const c = state.config, t = state.totals, by = t.by_status || {};

  const jobs = state.running;
  showService({ running: jobs.length, queue: state.queue.length, watching: state.watching.length });

  document.getElementById("meta").innerHTML = [
    `<span class="chip">режим <b>слияние</b></span>`,
    `<span class="chip">опрос <b>${c.scan_interval_sec} с</b>, дописан через <b>${c.stable_sec} с</b> покоя</span>`,
    `<span class="chip">одновременно до <b>${c.workers}</b> ${plural(c.workers, "записи", "записей", "записей")}</span>`,
    `<span class="chip">модели <b>${esc(c.speech_model)}</b> + <b>${esc(c.summary_model)}</b></span>`,
    c.key_present ? "" : `<span class="chip alarm">${icon("alert", 14)} ключ API не задан</span>`,
    storageChips(state.storage),
  ].join("");
  // предупреждения о хранении: текстом и значком, не только цветом; меняются редко — перерисовываются только при смене
  const stored = state.storage.warnings.map(w => `<div class="nhint warn lines" role="alert">${icon("alert", 14)}<span><b>Внимание:</b> ${esc(w)}.
      Подробно — <a href="/install-guide">в инструкции по установке</a>, раздел «Резервная копия и восстановление».</span></div>`).join("");
  const box = document.getElementById("storagewarn");
  if (box.dataset.shown !== stored) { box.innerHTML = stored; box.dataset.shown = stored; }

  document.getElementById("tiles").innerHTML = [
    ["обработано", by.ok || 0, "ok", (by.ok || 0) > 0 ? "good" : ""],
    ["нет звука", by.no_audio || 0, "mute", (by.no_audio || 0) > 0 ? "warm" : ""],
    ["без протокола", by.no_summary || 0, "doc", (by.no_summary || 0) > 0 ? "warm" : ""],
    ["ошибок", by.error || 0, "alert", (by.error || 0) > 0 ? "hot" : ""],
    ["в работе", `${jobs.length} из ${c.workers}`, "play", ""],
    ["в очереди", state.queue.length, "clock", ""],
    ["копируется", state.watching.length, "copy", ""],
    // у каждой валюты своя плитка: рубли и доллары не складываются
    ...(Object.keys(t.cost).length ? Object.entries(t.cost).map(([cur, v]) => [`потрачено, ${sign(cur)}`, amount(v, cur), "coin", ""])
                                   : [["потрачено", "—", "coin", ""]]),
  ].map(([l, n, ic, cls]) => `
    <div class="tile ${cls}">
      <div class="top">${icon(ic, 14)}<span class="l">${l}</span></div>
      <div class="n">${esc(n)}</div>
    </div>`).join("");

  // записей в работе несколько: у каждой свой шаг и свой прогресс
  document.getElementById("running").innerHTML = !jobs.length
    ? `<div class="empty">${icon("idle", 17)}простой — жду появления записей во входной папке</div>`
    : jobs.map(job => `<div class="job">
       <dl class="kv">
         <dt>файл</dt><dd class="path">${esc(job.file)}</dd>
         <dt>шаблон</dt><dd>${esc(state.templates[job.template] || "определяется по записи")}</dd>
         <dt>начато</dt><dd class="num">${hhmm(job.started_at)}</dd>
         <dt>запуск</dt><dd>${job.manual ? "вручную администратором" : "автоматически"}${job.force ? ", с перезаписью" : ""}${
           job.model ? `, распознавание — ${esc(modelTitle(job.model))}` : ""}</dd>
         <dt>шаг</dt><dd>${esc(job.step || "готовлю звук")}</dd>
       </dl>` +
      (job.chunks_total ? `
        <div class="bar" role="progressbar" aria-label="Распознано частей записи ${esc(job.file)}"
             aria-valuemin="0" aria-valuemax="${job.chunks_total}"
             aria-valuenow="${job.chunks_done}"><i style="width:${100 * job.chunks_done / job.chunks_total}%"></i></div>
        <div class="dim" style="font-size:12.5px;margin-top:6px">часть ${job.chunks_done} из ${job.chunks_total}</div>` : "") +
      `</div>`).join("");

  let q = "";
  if (state.queue.length) {
    q += `<div class="scroll"><table><thead><tr><th>в очереди</th><th>шаблон</th><th>запуск</th></tr></thead><tbody>` +
      state.queue.map(i => `<tr>
        <td class="path">${esc(i.path)}</td>
        <td>${esc(state.templates[i.template] || "по разбору записи")}</td>
        <td class="dim">${i.manual ? "вручную" : "авто"}${i.force ? ", перезапись" : ""}${
          i.model ? `, ${esc(modelTitle(i.model))}` : ""}${
          jobs.some(j => j.file === i.path) ? ", после текущего прогона этой записи" : ""}</td></tr>`).join("") +
      `</tbody></table></div>`;
  }
  if (state.watching.length) {
    q += `<div class="scroll" style="margin-top:${state.queue.length ? "12px" : "0"}">
      <table><thead><tr><th>копируется</th><th>размер</th><th>не меняется</th></tr></thead><tbody>` +
      state.watching.map(w => `<tr>
        <td class="path">${esc(w.path)}</td>
        <td class="num">${mb(w.size)}</td>
        <td class="num">${w.quiet_sec} с из ${w.stable_sec}
          <span class="dim">(замеров ${w.stable} из ${w.needed})</span></td></tr>`).join("") +
      `</tbody></table></div>`;
  }
  document.getElementById("queue").innerHTML = q ||
    `<div class="empty">${icon("clock", 17)}очередь пуста, новых файлов нет</div>`;

  document.getElementById("history").innerHTML = !history.length
    ? `<div class="empty">${icon("doc", 17)}пока ничего не обработано — положите запись во входную папку</div>`
    : `<div class="scroll"><table><thead><tr>
         <th>файл</th><th>статус</th><th>завершено</th><th>заняло</th><th>запись</th>
         <th>говорящих</th><th>без метки</th><th>стоимость</th></tr></thead><tbody>` +
      history.map(h => {
        const tags = [];
        if (h.manual) tags.push(`<span class="tag">вручную</span>`);
        if (h.force) tags.push(`<span class="tag">перезапись</span>`);
        if (h.reused_transcript) tags.push(`<span class="tag">расшифровка готовая</span>`);
        if (h.speakers_unreliable) tags.push(`<span class="tag warn">метки ненадёжны</span>`);
        if (h.crowded_meeting) tags.push(`<span class="tag warn">многолюдно</span>`);
        return `<tr>
          <td><a class="open" data-file="${esc(h.file)}" role="button" tabindex="0"
                 title="Открыть протокол и расшифровку"><span class="path">${esc(h.file)}</span></a>
              ${tags.length ? `<div>${tags.join("")}</div>` : ""}
              ${h.speech_model ? `<div class="dim" style="font-size:12px">распознавание: ${esc(modelTitle(h.speech_model))}</div>` : ""}
              ${h.error ? `<div class="err-text">${esc(h.error)}</div>` : ""}</td>
          <td><span class="badge ${esc(h.status)}">${icon(STATUS_ICONS[h.status] || "alert", 13)}${esc(STATUS_LABELS[h.status] || h.status)}</span></td>
          <td class="num">${hhmm(h.finished_at)}</td>
          <td class="num">${dur(h.duration_sec)}</td>
          <td class="num">${dur(h.audio_duration_sec)}</td>
          <td class="num">${h.speakers ?? "—"}</td>
          <td class="num">${h.unlabeled == null ? "—" : Math.round(h.unlabeled * 100) + "%"}</td>
          <td class="num">${money(h.cost)}${h.runs > 1
              ? `<div class="dim" style="font-size:12px">запусков: ${h.runs}, всего ${money(h.cost_total)}</div>` : ""}</td>
        </tr>`;
      }).join("") + `</tbody></table></div>`;

  const logEl = document.getElementById("log");
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  logEl.textContent = logLines.join("\n");
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;

  renderSpeech();
  renderProviders();
  renderAdmin();
  syncTemplates();
}

/* ---------- распознавание и поставщики ----------
   Модель для новых записей выбирает администратор; пока не выбрал — действует SPEECH_MODEL из .env.
   Форма перерисовывается, только когда на сервере что-то изменилось: страница опрашивает сервис
   каждые три секунды, и иначе список закрывался бы под рукой. */
const modelTitle = id => ((state && state.speech.models.find(m => m.id === id)) || { title: id }).title;
const modelPrice = m => `${String(m.price_hour).replace(".", ",")} $ за час`;
let speechSeen = "";

function renderSpeech() {
  const sp = state.speech, seen = JSON.stringify(sp);
  if (seen === speechSeen) return;
  speechSeen = seen;
  const source = sp.chosen
    ? `выбрано на странице: ${esc(sp.chosen.by)}, ${when(sp.chosen.at)}`
    : `из <code>.env</code> (SPEECH_MODEL)`;
  document.getElementById("speech").innerHTML = `
    <label for="smodel" style="margin-top:0">Модель распознавания для новых записей</label>
    <select id="smodel" aria-describedby="smodel-note smodel-src">${sp.models.map(m =>
      `<option value="${esc(m.id)}"${m.id === sp.current ? " selected" : ""}>${esc(m.title)} — ${modelPrice(m)}</option>`).join("")}</select>
    <div class="fhint" id="smodel-note"></div>
    <div class="fhint" id="smodel-src">Сейчас действует <b>${esc(modelTitle(sp.current))}</b> — ${source}.</div>
    <div class="row"><button type="button" id="ssave" disabled>Сохранить</button>
      ${sp.chosen ? `<button type="button" class="ghost" id="sreset">Вернуть значение из .env — ${esc(modelTitle(sp.env))}</button>` : ""}</div>
    <div class="msg" id="smsg" role="status"></div>
    <div class="note">Действует со следующей новой записи, без перезапуска. Уже распознанные записи не меняются:
      пересборка протокола берёт готовую расшифровку, какой бы моделью она ни была сделана. Распознать старую
      запись другой моделью — ручной запуск с перезаписью. Цена — справочная, в долларах у OpenRouter;
      счёт идёт по тому, что вернул поставщик.</div>`;
  speechNote();
}

function speechNote() {
  const sel = document.getElementById("smodel"), sp = state.speech;
  const m = sp.models.find(x => x.id === sel.value);
  document.getElementById("smodel-note").textContent = m.note.charAt(0).toUpperCase() + m.note.slice(1) + ".";
  document.getElementById("ssave").disabled = sel.value === sp.current;
}

async function saveSpeech(model) {
  say("smsg", "", "Сохраняю…");
  try {
    state.speech = await api("/api/admin/speech-model", { model });
    renderSpeech();
    refreshRunButton();
    say("smsg", "ok", model ? `Сохранено: новые записи распознаёт ${modelTitle(model)}.`
                            : `Возвращено значение из .env: ${modelTitle(state.speech.current)}.`);
  } catch (e) { say("smsg", "err", e.message); }
}
document.getElementById("speech").addEventListener("change", e => { if (e.target.id === "smodel") speechNote(); });
document.getElementById("speech").addEventListener("click", e => {
  if (e.target.id === "ssave") saveSpeech(document.getElementById("smodel").value);
  else if (e.target.id === "sreset") saveSpeech(null);
});

function renderProviders() {
  const p = state.providers, last = p.last_switch;
  const line = x => `<b>${esc(x.title)}</b> <span class="dim">· стоимость в ${sign(x.currency)} · ${esc(x.host)}</span>${
    x.key_present ? "" : ` <span class="tag warn">ключ не задан</span>`}`;
  document.getElementById("providers").innerHTML = `
    <dl class="kv provs">
      <dt>основной</dt><dd>${line(p.main)}</dd>
      <dt>запасной</dt><dd>${p.backup ? line(p.backup)
        : `<span class="dim">не настроен — BACKUP_BASE_URL и BACKUP_API_KEY в .env</span>`}</dd>
      ${p.text ? `<dt>текстовые этапы</dt><dd>${line(p.text)}<div class="dim">свой адрес — запасной их не подхватывает</div></dd>` : ""}
      ${p.backup ? `<dt>уход к запасному</dt><dd>${last
        ? `${when(last.at)}${last.refused ? ` <span class="tag warn">${icon("alert", 12)} отказ по ключу или балансу</span>` : ""}
           <div class="dim">${esc(last.reason)}</div>
           ${p.resting_sec ? `<div>Сейчас запросы идут сразу запасному; основной попробуем снова через ${p.resting_sec} с.</div>` : ""}`
        : `<span class="dim">с запуска сервиса не было</span>`}</dd>` : ""}
    </dl>
    <div class="note">Запасному уходит вызов, на который основной не ответил после повторов (сеть, ошибка
      сервера, «слишком много запросов») или сразу отказал по ключу и балансу. Отказ на сам запрос и таймаут
      ожидания ответа на распознавание запасному не уходят: поставщик мог уже выполнить и списать работу —
      запись повторится по расписанию, готовые части возьмутся из сохранённых ответов. Расход записывается
      в валюте того, кто ответил.</div>`;
}

/* ---------- проверка ключа поставщика ----------
   Неверный ключ иначе виден только на первой записи. Кнопка шлёт поставщикам бесплатные запросы
   (сведения о ключе, список моделей) и показывает ответ по каждому настроенному: основному,
   запасному и своему адресу текстовых этапов. Блок рисуется сам по себе, а не вместе со сводкой
   поставщиков: ту страница перерисовывает каждые три секунды, и кнопка теряла бы фокус. */
let keyReport = null, keyBusy = false;
const KEY_MARKS = { ok: ["good", "ok", "ключ принят"], warn: ["soso", "info", "внимание"],
                    refused: ["bad", "alert", "отказ"], down: ["soso", "alert", "нет ответа"] };

function renderKeyCheck() {
  const report = !keyReport ? "" : `<ol class="steps" aria-label="Итог проверки ключа">` + keyReport.checks.map(c => {
      const [cls, ic, word] = KEY_MARKS[c.state];
      return `<li><span class="${cls}">${icon(ic, 17)}</span><div class="body"><b>${esc(c.role.charAt(0).toUpperCase() + c.role.slice(1))}${c.title ? ` — ${esc(c.title)}` : ""}</b>
        <span class="verdict ${cls}">${word}</span><div>${esc(c.text.charAt(0).toUpperCase() + c.text.slice(1))}.</div>
        ${c.hint ? `<div class="todo"><b>Что делать:</b> ${esc(c.hint)}.</div>` : ""}</div></li>`;
    }).join("") + `</ol>`;
  document.getElementById("keycheck").innerHTML = `
    <div class="row"><button type="button" class="ghost" id="keygo" aria-describedby="keygo-hint" ${keyBusy ? "disabled" : ""}>${
      keyBusy ? "Проверяю…" : "Проверить ключ"}</button></div>
    <div class="fhint" id="keygo-hint">Бесплатный запрос к поставщику: принят ли ключ и есть ли у него текстовые модели
      из <code>.env</code>. Записи не обрабатываются, деньги не списываются.</div>
    <div class="msg ${keyReport ? (keyReport.ok ? "ok" : "err") : ""}" id="keymsg" role="status">${keyBusy ? "Спрашиваю поставщика…"
      : !keyReport ? "" : keyReport.ok ? "Проверка пройдена." : "Есть замечания — у каждого написано, что делать."}</div>${report}`;
}

document.getElementById("keycheck").addEventListener("click", async e => {
  if (e.target.id !== "keygo" || keyBusy) return;
  keyBusy = true; keyReport = null;
  renderKeyCheck();
  try {
    keyReport = await api("/api/admin/providers/check", {});
  } catch (err) {
    keyReport = { ok: false, checks: [{ role: "Проверка не выполнена", title: "", state: "down", text: err.message, hint: "" }] };
  }
  keyBusy = false;
  renderKeyCheck();
  document.getElementById("keygo").focus();
});
renderKeyCheck();

function renderAdmin() {
  const box = document.getElementById("admin");
  if (box.dataset.mode === "unlocked") { refreshRunButton(); return; }
  box.dataset.mode = "unlocked";
  box.innerHTML = `
    <label for="file" style="margin-top:0">Файл из входной папки</label>
    <select id="file"></select>
    <label for="tpl">Шаблон протокола</label>
    <select id="tpl"></select>
    <div class="fhint">«Определить по записи» — так же, как работает автоматика: тип встречи выбирается
      разбором расшифровки. Свой шаблон нужен, только если разбор ошибся.</div>
    <label class="check" for="force"><input type="checkbox" id="force" aria-describedby="force-hint"> перезаписать готовый результат</label>
    <div class="fhint" id="force-hint">Без перезаписи готовая расшифровка переиспользуется — заново делается только
      протокол, и платить за распознавание второй раз не придётся.</div>
    <label for="rmodel">Модель распознавания для этого запуска</label>
    <select id="rmodel" aria-describedby="rmodel-hint" disabled></select>
    <div class="fhint" id="rmodel-hint"></div>
    <div class="row"><button id="run">Запустить обработку</button>
      <button class="ghost" id="reload">Обновить список</button></div>
    <div class="msg" id="amsg"></div>`;
  document.getElementById("run").addEventListener("click", run);
  document.getElementById("force").addEventListener("change", refreshRunButton);
  document.getElementById("reload").addEventListener("click", () => loadFiles(true));
  loadFiles(true);
}

/* Список шаблонов ручного запуска — из типов, какими их знает сервис сейчас: свой тип,
   добавленный в «Настройках», появляется здесь без перезагрузки страницы. */
function syncTemplates() {
  const sel = document.getElementById("tpl");
  if (!sel) return;
  const sig = JSON.stringify(state.templates);
  if (sel.dataset.sig === sig) return;
  const keep = sel.value;
  sel.innerHTML = `<option value="">определить по записи</option>` +
    Object.entries(state.templates).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  sel.value = keep in state.templates ? keep : "";
  sel.dataset.sig = sig;
}

/* ---------- пользователи ----------
   Пока их нет, сервис открыт всем. С первым пользователем вход становится
   обязательным: каждый видит свою личную папку и общие, которые ему открыты. */
let people = null, editing = null;      // editing: логин в форме, "" — новый, null — форма закрыта
let claims = [], mailState = null;      // заявки, ждущие решения, и состояние почты
let claimAct = null;                    // решение по заявке: {id, kind: "approve" | "reject"}
let userAct = null;                     // подтверждение удаления: логин
let bindAct = null;                     // подтверждение привязки к домену: логин
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
      list.map(u => `<tr${u.login === userAct || u.login === editing || u.login === bindAct ? ' class="picked"' : ""}>
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
               aria-label="Убрать из сервиса пользователя ${esc(u.login)}" title="Убрать из сервиса. Учётная запись домена: при следующем входе заведётся снова, если человек остался в домене и в группе доступа">Убрать</button>`
          : `<button class="ghost" data-edit="${esc(u.login)}">Изменить</button>
            ${mailOn && u.email ? `<button class="ghost" data-link="${esc(u.login)}"
              title="Отправить на ${esc(u.email)} новую ссылку «задать пароль»; прежняя перестанет действовать">Выслать ссылку</button>`
            : `<button class="ghost" data-link="${esc(u.login)}"
              title="Выпустить новую одноразовую ссылку «задать пароль» и показать её здесь — передать человеку самому; прежняя перестанет действовать">Показать ссылку</button>`}
            <button class="ghost dangerlink" data-del="${esc(u.login)}">Удалить</button>`}</div></td>
      </tr>`).join("") + `</tbody></table></div>` : "") +
    (dom && dom.on && list.some(u => !foreign(u)) ? `<div class="note" style="margin-top:12px">Локальная учётная запись сама доменной не становится.
      Пока она есть, человек с тем же логином в домене войти доменным паролем не может: логин занят. «Привязать к домену» (кнопка «Изменить» в строке пользователя) переводит
      запись на вход через домен с сохранением прав.</div>` : "") +
    confirmBox + bindBox +
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
  if (t.id === "uadd") { editing = ""; userAct = null; usersNote = null; renderUsers(); document.getElementById("ulogin").focus(); }
  else if (t.id === "ucancel") { editing = null; renderUsers(); }
  else if (t.id === "ugen") { document.getElementById("upass").value = newPassword(); fieldError("upass", ""); }
  else if (t.id === "ucopy") { copyText(document.getElementById("upass").value); t.textContent = "Скопировано";
                               setTimeout(() => { t.textContent = "Копировать"; }, 2000); }
  else if (t.dataset.edit) { editing = t.dataset.edit; userAct = null; usersNote = null; renderUsers(); document.getElementById("uname").focus(); }
  else if (t.dataset.del) { userAct = t.dataset.del; bindAct = null; editing = null; usersNote = null; renderUsers(); document.getElementById("udelete").focus(); }
  else if (t.id === "udelcancel") { userAct = null; renderUsers(); }
  else if (t.dataset.bind) { bindAct = t.dataset.bind; userAct = null; editing = null; usersNote = null; renderUsers(); document.getElementById("ubindcancel").focus(); }
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

function amsg(text, cls) {
  const el = document.getElementById("amsg");
  if (el) { el.textContent = text; el.className = "msg " + (cls || ""); }
}

async function loadFiles(force) {
  const sel = document.getElementById("file");
  if (!sel) return;
  if (filesCache && !force) return fillFiles(filesCache);
  try {
    filesCache = (await api("/api/admin/files", {})).files;
    fillFiles(filesCache);
  } catch (e) {
    amsg(e.message, "err");
  }
}

function fillFiles(files) {
  const sel = document.getElementById("file");
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = files.length
    ? files.map(f => `<option value="${esc(f.path)}">${esc(f.path)} — ${mb(f.size)}` +
        (f.status ? ` — ${STATUS_LABELS[f.status] || f.status}` : " — не обрабатывался") + "</option>").join("")
    : '<option value="">во входной папке нет медиафайлов</option>';
  if (keep && files.some(f => f.path === keep)) sel.value = keep;
  refreshRunButton();
}

function refreshRunButton() {
  const btn = document.getElementById("run"), sel = document.getElementById("file");
  if (btn && sel) btn.disabled = busy || !sel.value;
  // Модель выбирается только вместе с перезаписью: без неё распознавание не запускается вовсе.
  const model = document.getElementById("rmodel"), force = document.getElementById("force");
  if (!model) return;
  const sp = state.speech, keep = model.value;
  model.innerHTML = `<option value="">как в настройках — ${esc(modelTitle(sp.current))}</option>` +
    sp.models.map(m => `<option value="${esc(m.id)}">${esc(m.title)} — ${modelPrice(m)}</option>`).join("");
  model.value = force.checked ? keep : "";
  model.disabled = !force.checked;
  const hint = document.getElementById("rmodel-hint");
  hint.className = force.checked ? "fhint warnhint" : "fhint";
  hint.innerHTML = force.checked
    ? `${icon("alert", 14)}<span>Запись будет распознана заново — за деньги, по цене выбранной модели.
        Ручные подписи говорящих сбросятся: у новой расшифровки другие метки.</span>`
    : "Выбор доступен только с перезаписью: без неё берётся готовая расшифровка, и распознавание не запускается.";
}

async function run() {
  const path = document.getElementById("file").value;
  const template = document.getElementById("tpl").value;
  const force = document.getElementById("force").checked;
  const model = force ? document.getElementById("rmodel").value : "";
  if (!path) return;
  busy = true; refreshRunButton();
  amsg("отправляю…", "");
  try {
    await api("/api/admin/run", { file: path, template: template || null, force, model: model || null });
    amsg("файл поставлен в начало очереди", "ok");
    tick();
  } catch (e) {
    amsg(e.message, "err");
  } finally {
    busy = false; refreshRunButton();
  }
}

// Страница — только администратору сервиса: остальных возвращаем к записям.
async function restore() {
  await initUser();
  const never = new Promise(() => {});          // страница уходит на другой адрес — дальше ничего не делаем
  if (!me || !me.login) return never;           // на вход уже отправил initUser
  if (!me.admin) { location.href = "/"; return never; }
  // в личном режиме пользователей и общих папок нет — остаётся один раздел, вкладки не нужны
  if (me.personal) document.querySelector(".admintabs").hidden = true;
}

/* ---------- разделы ----------
   Записи — что сервис делает и сделал; папки и доступ — общие папки и кому они открыты;
   пользователи — учётные записи. Раздел живёт в адресе: ссылку можно сохранить. */
const PANES = ["records", "folders", "users", "costs", "templates", "brand"];
const SETTINGS = ["templates", "brand"];         // два раздела под одним пунктом меню «Настройки»

function showPane(name) {
  if (me && me.personal && name === "brand") name = "templates";      // в личном режиме оформление не меняется
  if (!PANES.includes(name) || (me && me.personal && !["costs", "templates"].includes(name))) name = "records";
  if (tplDirty() && name !== "templates") {
    // несохранённую правку шаблона не теряем молча: остаёмся в разделе и говорим почему
    window.history.replaceState(null, "", "#templates");
    say("tplmsg", "err", "Правка не сохранена: сохраните её или нажмите «Отменить правку» — и раздел откроется.");
    return;
  }
  document.querySelectorAll("[data-pane]").forEach(s => { s.hidden = s.dataset.pane !== name; });
  document.querySelectorAll(".admintabs button").forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  // window.history: на этой странице есть своя переменная history — история обработки
  if (location.hash.slice(1) !== name) window.history.replaceState(null, "", "#" + name);
  if (name === "folders") loadFolders();
  if (name === "users") loadUsers();
  if (name === "costs") loadCosts();
  if (name === "templates") loadTemplates();
  if (name === "brand") loadBrand();
  const tabs = document.getElementById("settabs");
  tabs.hidden = !SETTINGS.includes(name) || !!(me && me.personal);
  tabs.querySelectorAll("button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.set === name)));
  renderNav();                       // подсветить открытый раздел в меню
  document.getElementById("pagetitle").textContent =
    { records: "Обработка", folders: "Папки и доступ", users: "Пользователи", costs: "Расходы",
      templates: me && me.personal ? "Настройки: шаблоны и словарь" : "Настройки", brand: "Настройки" }[name];
}
document.getElementById("settabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-set]");
  if (b) showPane(b.dataset.set);
});
addEventListener("beforeunload", e => { if (tplDirty()) e.preventDefault(); });
document.querySelector(".admintabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-tab]");
  if (b) showPane(b.dataset.tab);
});
addEventListener("hashchange", () => showPane(location.hash.slice(1)));

/* ---------- настройки: шаблоны, задания модели, словарь ----------
   Страница показывает то, что лежит в файлах папки шаблонов сейчас: их правят и в обход
   страницы. Текст открывается в поле правки; сохранение проверяет сервер — обязательное
   не пропустит, о желательном предупредит. Прежний текст остаётся в прошлых версиях. */
let tpl = null;             // ответ /api/admin/templates
let tplOpen = null;         // открытый файл: {id, text — как сохранён, side — что показано рядом, ask — вопрос}
let tplNew = false;         // открыта форма нового типа
const stamp = iso => iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)} ${iso.slice(11, 16)}` : "";
const stampSec = iso => iso ? `${stamp(iso)}:${iso.slice(17, 19)}` : "";
const lf = text => String(text || "").replace(/\r\n?/g, "\n");
const tplItem = id => tpl && tpl.items.find(t => t.id === id);

function tplDirty() {
  const field = document.getElementById("tpltext");
  return !!(tplOpen && field && lf(field.value) !== tplOpen.text);
}

async function loadTemplates() {
  try { tpl = await api("/api/admin/templates"); }
  catch (err) { document.getElementById("tplhead").innerHTML = `<div class="card"><div class="msg err">${esc(err.message)}</div></div>`; return; }
  renderTemplates();
}

const TPL_STATES = { standard: ["", "стандартный"], changed: ["warn", "изменён"], custom: ["", "свой тип"], missing: ["warn", "файла нет"] };

function tplTags(t) {
  const [cls, text] = TPL_STATES[t.state];
  return `<span class="tag ${cls}">${text}</span>` +
    (t.outside ? ` <span class="tag warn" title="Текст в файле не тот, что сохраняли на странице">правка файлом</span>` : "") +
    // у нетронутого стандартного шаблона счётчика нет: его «замечания» — не недочёты, а то, как задуман
    // этот тип (в протоколе лекции нет раздела «Задачи»); они написаны в самом шаблоне, когда он открыт
    (t.problem ? ` <span class="tag warn">не годится</span>`
      : t.warnings.length && t.state !== "standard" ? ` <span class="tag">замечаний: ${t.warnings.length}</span>` : "");
}

function tplTable(items, first) {
  return `<div class="scroll"><table class="tpllist"><thead><tr><th>${first}</th><th>когда применяется</th><th>состояние</th>
      <th>изменён</th><th></th></tr></thead><tbody>${items.map(t => `
    <tr class="${tplOpen && tplOpen.id === t.id ? "picked" : ""}">
      <td><b>${esc(t.title)}</b><div class="dim mono">${esc(t.file)}${t.kind === "type" ? ` · ключ ${esc(t.id)}` : ""}</div></td>
      <td class="when">${esc(t.when)}${t.actions ? `<div class="dim">Поручения выписываются отдельным шагом.</div>` : ""}</td>
      <td>${tplTags(t)}</td>
      <td class="num">${t.state === "standard" && !t.by ? `<span class="dim">—</span>`
        : `${esc(t.by || (t.outside ? "не на странице" : ""))}<div class="dim">${stamp(t.changed_at)}</div>`}</td>
      <td><div class="rowacts"><button class="ghost" data-open="${esc(t.id)}">${tplOpen && tplOpen.id === t.id ? "Открыт" : "Открыть"}</button></div></td>
    </tr>`).join("")}</tbody></table></div>`;
}

function renderTemplates() {
  const types = tpl.items.filter(t => t.kind === "type"), service = tpl.items.filter(t => t.kind !== "type");
  document.getElementById("tplhead").innerHTML = `
    <div class="note" style="margin:0 0 var(--gap)">Шаблон — это задание модели: по нему она пишет протокол. Правка действует
      со следующей записи, без перезапуска. <b>Готовые протоколы не меняются.</b> Чтобы пересобрать одну запись по новому
      тексту, откройте <a href="#records">«Обработка» → «Ручной запуск»</a>, выберите файл и запустите без галки
      «перезаписать»: распознавание не повторяется, заново делается только протокол.</div>
    ${tpl.problem ? `<div class="nhint warn">${icon("alert", 14)}<span>${esc(tpl.problem)}.</span></div>` : ""}
    ${tpl.writable ? "" : `<div class="nhint warn">${icon("alert", 14)}<span>Папка шаблонов подключена только для чтения:
      сохранить правку сервис не сможет. Уберите <code>:ro</code> у папки шаблонов в compose.yml.</span></div>`}`;
  document.getElementById("tpltypes").innerHTML = tplTable(types, "тип записи") + `
    <div class="row"><button class="ghost" id="tpladd" ${tplNew ? "disabled" : ""}>${icon("plus", 15)} Добавить свой тип</button>
      <span class="fhint" style="margin:0">Тип выбирает разбор записи — по описанию «когда применяется».</span></div>`;
  document.getElementById("tplservice").innerHTML = tplTable(service, "файл");
  const card = document.getElementById("tplnewcard"), form = document.getElementById("tplnew");
  card.hidden = !tplNew;
  if (tplNew && !form.dataset.drawn) {
    form.dataset.drawn = "1";
    form.innerHTML = `
      <p class="licwhy" style="margin-top:0">Свой тип — это название, описание «когда применять» и текст шаблона.
        После добавления разбор записи выбирает и из него. Текст шаблона откроется для правки сразу после добавления.</p>
      ${formField("ntitle", "Название", `<input type="text" id="ntitle" maxlength="${tpl.limits.title}" autocomplete="off"
        aria-describedby="ntitle-hint ntitle-err">`, "По-русски, как оно будет стоять в списках: «Разбор инцидента», «Собеседование».", true)}
      ${formField("nwhen", "Когда применять", `<textarea id="nwhen" rows="3" maxlength="${tpl.limits.when}"
        aria-describedby="nwhen-hint nwhen-err"></textarea>`,
        "Одно-два предложения о том, как построен разговор, а не о теме: по ним разбор отличит этот тип от остальных.", true)}
      ${formField("nbase", "Взять текст за основу", `<select id="nbase">${types.filter(t => t.exists).map(t =>
        `<option value="${esc(t.id)}">${esc(t.title)}</option>`).join("")}</select>`,
        "Шаблон нового типа начнётся с копии выбранного — дальше правьте под себя.")}
      <label class="check" for="nactions"><input type="checkbox" id="nactions" aria-describedby="nactions-hint"> выписывать поручения отдельным шагом</label>
      <div class="fhint" id="nactions-hint">Как у совещания: перед протоколом две модели независимо выписывают поручения,
        и шаблон получает их список. Точнее, но дороже на два вызова. Нужен, если шаблон на этот список рассчитан.</div>
      ${formField("nkey", "Ключ", `<input type="text" id="nkey" maxlength="30" autocomplete="off" autocapitalize="none"
        spellcheck="false" class="mono" aria-describedby="nkey-hint nkey-err">`,
        "Необязательно: латиницей, для имени файла и ответов API. Пусто — сервис составит его из названия.")}
      <div class="row"><button id="ngo">Добавить тип</button><button type="button" class="ghost" id="ncancel">Отмена</button></div>
      <div class="msg" id="nmsg" role="status"></div>`;
    const sync = () => { document.getElementById("nactions").checked = !!(tplItem(document.getElementById("nbase").value) || {}).actions; };
    document.getElementById("nbase").onchange = sync;
    sync();
  }
  if (!tplNew) { form.innerHTML = ""; delete form.dataset.drawn; }
}

function tplAsk(t) {
  const ask = tplOpen.ask;
  if (!ask) return "";
  const box = (title, items, go, label, danger) => `<div class="up ${danger ? "dangerbox" : ""}" role="alertdialog" aria-labelledby="tplasktitle">
      <div class="uphead"><span class="upname" id="tplasktitle">${title}</span></div>
      <ul class="plain">${items.map(i => `<li>${i}</li>`).join("")}</ul>
      <div class="row"><button type="button" class="${danger ? "danger" : ""}" data-ed="${go}">${label}</button>
        <button type="button" class="ghost" data-ed="ask-cancel" id="tplaskno">Отмена</button></div></div>`;
  const kept = `текущий текст останется в прошлых версиях — вернуть его можно здесь же;`;
  const after = `готовые протоколы не изменятся: по новому тексту пойдут записи, обработанные после этого.`;
  if (ask.kind === "default") return box(`Вернуть стандартный текст — «${esc(t.title)}»?`,
    [`в файл ${esc(t.file)} будет записан текст из поставки сервиса;`, kept, after], "do-default", "Вернуть стандартный");
  if (ask.kind === "backup") return box(`Вернуть версию от ${stampSec(ask.at)}?`,
    [`в файл ${esc(t.file)} будет записан текст этой версии;`, kept, after], "do-backup", "Вернуть эту версию");
  if (ask.kind === "file") return box(`Заменить текст содержимым файла «${esc(ask.file.name)}» (${mb(ask.file.size)})?`,
    [`файл должен быть текстом в кодировке UTF-8; сервер проверит его так же, как правку на странице;`, kept, after], "do-file", "Заменить текст");
  return box(`Удалить тип «${esc(t.title)}»?`,
    [`разбор записи перестанет его выбирать, из списка ручного запуска он пропадёт;`,
     `записи, уже обработанные по нему, останутся как есть — их протоколы не тронуты;`,
     `при пересборке такой записи шаблон заново выберет разбор из оставшихся типов;`,
     `текст шаблона уйдёт в папку history рядом с шаблонами — вернуть его можно только с сервера.`],
    "do-delete", "Удалить тип", true);
}

function renderEditor(value) {
  const card = document.getElementById("tpleditcard");
  card.hidden = !tplOpen;
  if (!tplOpen) { document.getElementById("tpledit").innerHTML = ""; return; }
  const t = tplItem(tplOpen.id), side = tplOpen.side, id = encodeURIComponent(t.id);
  document.getElementById("tpledit").innerHTML = `
    <div class="edhead"><h2>${esc(t.title)}</h2><span class="dim mono">${esc(t.file)}</span><span>${tplTags(t)}</span>
      <button class="ghost icon" data-ed="close" aria-label="Закрыть" title="Закрыть">${icon("close", 16)}</button></div>
    ${t.custom ? `<form class="eddesc" id="tpldesc" novalidate>
      ${formField("dtitle", "Название типа", `<input type="text" id="dtitle" maxlength="${tpl.limits.title}" value="${esc(t.title)}" aria-describedby="dtitle-err">`, "", true)}
      ${formField("dwhen", "Когда применять", `<textarea id="dwhen" rows="2" maxlength="${tpl.limits.when}" aria-describedby="dwhen-hint dwhen-err">${esc(t.when)}</textarea>`,
        "По этому описанию разбор записи выбирает тип.", true)}
      <label class="check" for="dactions"><input type="checkbox" id="dactions" ${t.actions ? "checked" : ""}> выписывать поручения отдельным шагом</label>
      <div class="row"><button class="ghost" id="dsave">Сохранить описание</button></div>
      <div class="msg" id="dmsg" role="status"></div></form>` : ""}
    ${t.problem ? `<div class="nhint warn">${icon("alert", 14)}<span><b>С этим текстом сервис работать не сможет:</b> ${esc(t.problem)}.</span></div>` : ""}
    ${t.outside ? `<div class="nhint">${icon("info", 14)}<span>Текст в файле не тот, что сохраняли на странице: его правили
      на сервере, в обход страницы. Ниже — то, что лежит в файле сейчас.</span></div>` : ""}
    ${!t.warnings.length ? "" : t.state === "standard"
      ? `<div class="edwarn calm" role="note"><b>${icon("info", 14)} Так задумано: чего нет в протоколе этого типа</b>
      <ul class="plain">${t.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>`
      : `<div class="edwarn" role="status"><b>${icon("alert", 14)} На что обратить внимание</b>
      <ul class="plain">${t.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul></div>`}
    <div class="edgrid ${side ? "two" : ""}">
      <div>
        <label for="tpltext">Текст в файле сейчас</label>
        <textarea id="tpltext" class="tpltext" rows="24" spellcheck="false" autocomplete="off" autocapitalize="none"
                  aria-describedby="tplcount tpltext-err"></textarea>
        <div class="fhint" id="tplcount"></div>
        <div class="ferr" id="tpltext-err" role="alert"></div>
      </div>
      ${side ? `<div>
        <div class="sidehead"><label id="tplsidelabel">${esc(side.label)}</label>
          <button class="ghost" data-ed="unside">Скрыть</button></div>
        <pre class="tplside" tabindex="0" aria-labelledby="tplsidelabel">${esc(side.text)}</pre>
        <div class="fhint">Только для чтения: сравните и перенесите нужное.</div>
      </div>` : ""}
    </div>
    <div class="row">
      <button data-ed="save">Сохранить</button>
      <button class="ghost" data-ed="revert">Отменить правку</button>
      ${t.exists ? `<a class="linkbtn" href="/api/admin/templates/text?id=${id}&download=true" download="${esc(t.file)}">${icon("copy", 15)} Скачать</a>` : ""}
      <button class="ghost" data-ed="pick">${icon("upload", 15)} Загрузить файл…</button>
      <input type="file" id="tplfile" accept=".md,.txt,text/plain,text/markdown" hidden>
      ${t.has_default && !(side && side.kind === "default") ? `<button class="ghost" data-ed="side-default">Показать стандартный рядом</button>` : ""}
      ${t.has_default && t.state !== "standard" ? `<button class="ghost dangerlink" data-ed="ask-default">${icon("undo", 15)} Вернуть стандартный</button>` : ""}
    </div>
    <div class="fhint">Файл — .md или .txt в кодировке UTF-8, до ${Math.round(tpl.limits.upload_bytes / 1024)} КБ; текст — до ${tpl.limits.chars.toLocaleString("ru")} знаков.</div>
    <div class="msg" id="tplmsg" role="status"></div>
    ${tplAsk(t)}
    <h3 class="edsub">Прошлые версии</h3>
    ${t.backups.length ? `<ul class="edvers">${t.backups.map(b => `<li><span>${stampSec(b.at)} <span class="dim">· ${mb(b.bytes)}</span></span>
        <span class="rowacts"><button class="ghost" data-ed="side-backup" data-v="${esc(b.id)}" data-at="${esc(b.at)}">Показать рядом</button>
          <button class="ghost" data-ed="ask-backup" data-v="${esc(b.id)}" data-at="${esc(b.at)}">Вернуть</button></span></li>`).join("")}</ul>`
      : `<div class="empty">${icon("idle", 17)}прошлых версий пока нет</div>`}
    <div class="fhint">Прежний текст сохраняется при каждом изменении; хранятся ${tpl.limits.keep} последние версии
      (папка history рядом с шаблонами).</div>
    ${t.custom ? `<div class="row"><button class="ghost dangerlink" style="margin-left:0" data-ed="ask-delete">Удалить этот тип</button></div>` : ""}`;
  const field = document.getElementById("tpltext");
  field.value = value === undefined ? tplOpen.text : value;
  const count = () => {
    const n = field.value.length, over = n > tpl.limits.chars;
    document.getElementById("tplcount").textContent = `${n.toLocaleString("ru")} знаков из ${tpl.limits.chars.toLocaleString("ru")}` +
      (over ? " — слишком длинно" : "") + (tplDirty() ? " · есть несохранённая правка" : "");
  };
  field.addEventListener("input", count);
  count();
  const no = document.getElementById("tplaskno");
  if (no) { no.focus(); no.closest(".up").scrollIntoView({ block: "nearest" }); }
}

async function openTemplate(id) {
  if (tplDirty() && tplOpen.id !== id) {
    say("tplmsg", "err", "Правка не сохранена: сохраните её или нажмите «Отменить правку» — потом откройте другой файл.");
    document.getElementById("tpleditcard").scrollIntoView({ block: "nearest" });
    return;
  }
  const t = tplItem(id);
  let text = "";
  if (t.exists) {
    try { text = lf((await api(`/api/admin/templates/text?id=${encodeURIComponent(id)}`)).text); }
    catch (err) { say("tplmsg", "err", err.message); return; }
  }
  tplOpen = { id, text, side: null, ask: null };
  tplNew = false;
  renderTemplates();
  renderEditor();
  const card = document.getElementById("tpleditcard");
  card.scrollIntoView({ block: "start" });
  card.focus({ preventScroll: true });
}

/* Ответ сервера после изменения: список заново, текст открытого файла — из файла. */
async function tplAfter(d, said, kind) {
  tpl = d;
  if (tplOpen && tplItem(tplOpen.id)) {
    tplOpen.text = lf((await api(`/api/admin/templates/text?id=${encodeURIComponent(tplOpen.id)}`)).text);
    tplOpen.ask = null;
    renderEditor();
    say("tplmsg", kind || "ok", said);
  } else { tplOpen = null; renderEditor(); }
  renderTemplates();
  tick();                 // список шаблонов ручного запуска — сразу
}

const SAVED = "Новые записи пойдут по этому тексту; готовые протоколы не изменились.";

document.querySelector('[data-pane="templates"]').addEventListener("click", async e => {
  const open = e.target.closest("[data-open]");
  if (open) { openTemplate(open.dataset.open); return; }
  if (e.target.closest("#tpladd")) {
    tplNew = true; renderTemplates();
    document.getElementById("tplnewcard").scrollIntoView({ block: "nearest" });
    document.getElementById("ntitle").focus();
    return;
  }
  if (e.target.closest("#ncancel")) { tplNew = false; renderTemplates(); document.getElementById("tpladd").focus(); return; }
  const btn = e.target.closest("[data-ed]");
  if (!btn || !tplOpen) return;
  const act = btn.dataset.ed, field = document.getElementById("tpltext"), t = tplItem(tplOpen.id);
  const id = encodeURIComponent(t.id);
  const guard = async work => {
    btn.disabled = true;
    try { await work(); }
    catch (err) {
      btn.disabled = false;
      if (act === "save") { fieldError("tpltext", err.message.charAt(0).toUpperCase() + err.message.slice(1)); field.focus(); }
      else say("tplmsg", "err", err.message);
    }
  };
  if (act === "close") {
    if (tplDirty()) { say("tplmsg", "err", "Правка не сохранена: сохраните её или нажмите «Отменить правку»."); return; }
    tplOpen = null; renderEditor(); renderTemplates();
  }
  if (act === "revert") { tplOpen.ask = null; renderEditor(); say("tplmsg", "", "Правка отменена: в поле снова текст из файла."); }
  if (act === "save") {
    fieldError("tpltext", "");
    await guard(async () => {
      const d = await api("/api/admin/templates/save", { id: t.id, text: field.value });
      await tplAfter(d, d.changed ? "Сохранено. " + SAVED + (d.warnings.length ? " Обратите внимание на замечания выше." : "")
                                  : "Текст тот же, что в файле, — сохранять нечего.");
    });
  }
  if (act === "pick") document.getElementById("tplfile").click();
  if (act === "unside") { tplOpen.side = null; renderEditor(field.value); }
  if (act === "side-default" || act === "side-backup") {
    await guard(async () => {
      const version = act === "side-default" ? "default" : btn.dataset.v;
      const d = await api(`/api/admin/templates/text?id=${id}&version=${encodeURIComponent(version)}`);
      tplOpen.side = { kind: act === "side-default" ? "default" : "backup", text: lf(d.text),
                       label: act === "side-default" ? "Стандартный текст из поставки" : `Версия от ${stampSec(btn.dataset.at)}` };
      renderEditor(field.value);
    });
  }
  if (act === "ask-default" || act === "ask-delete" || act === "ask-backup") {
    if (tplDirty()) { say("tplmsg", "err", "Сначала сохраните правку или нажмите «Отменить правку»."); return; }
    tplOpen.ask = { kind: act.slice(4), version: btn.dataset.v, at: btn.dataset.at };
    renderEditor();
  }
  if (act === "ask-cancel") { tplOpen.ask = null; renderEditor(field.value); }
  if (act === "do-default" || act === "do-backup") {
    await guard(async () => {
      const d = await api("/api/admin/templates/restore", { id: t.id, version: act === "do-default" ? "default" : tplOpen.ask.version });
      tplOpen.side = null;
      await tplAfter(d, (act === "do-default" ? "Возвращён стандартный текст. " : "Прошлая версия возвращена. ") + SAVED);
    });
  }
  if (act === "do-file") {
    await guard(async () => {
      const d = await putFile(`/api/admin/templates/upload?id=${id}`, tplOpen.ask.file);
      await tplAfter(d, d.changed ? "Файл загружен. " + SAVED : "В файле тот же текст — менять нечего.");
    });
  }
  if (act === "do-delete") {
    await guard(async () => {
      const d = await api("/api/admin/templates/type/delete", { key: t.id });
      tplOpen = null;
      await tplAfter(d);
    });
  }
});

document.querySelector('[data-pane="templates"]').addEventListener("change", e => {
  if (e.target.id !== "tplfile" || !e.target.files.length || !tplOpen) return;
  const file = e.target.files[0];
  if (tplDirty()) { say("tplmsg", "err", "Сначала сохраните правку или нажмите «Отменить правку»."); return; }
  if (file.size > tpl.limits.upload_bytes) {
    say("tplmsg", "err", `Файл больше ${Math.round(tpl.limits.upload_bytes / 1024)} КБ (${mb(file.size)}) — шаблон такого размера сервис не принимает.`);
    return;
  }
  tplOpen.ask = { kind: "file", file };
  renderEditor();
});

document.querySelector('[data-pane="templates"]').addEventListener("submit", async e => {
  e.preventDefault();
  const fresh = e.target.id === "tplnew";
  const p = fresh ? "n" : "d", val = id => document.getElementById(p + id).value.trim();
  let bad = null;
  for (const [id, text] of [["title", "Укажите название типа"],
                            ["when", "Опишите, когда применять этот тип: одно-два предложения, не короче 20 знаков"]]) {
    const wrong = id === "when" ? val(id).length < 20 : !val(id);
    fieldError(p + id, wrong ? text : "");
    if (wrong && !bad) bad = p + id;
  }
  if (fresh && val("key") && !/^[a-z][a-z0-9_]{1,29}$/.test(val("key").toLowerCase())) {
    fieldError("nkey", "Ключ — латинские буквы, цифры и подчёркивание, от 2 до 30 знаков, первая — буква");
    bad = bad || "nkey";
  } else if (fresh) fieldError("nkey", "");
  if (bad) { document.getElementById(bad).focus(); return; }
  const btn = document.getElementById(fresh ? "ngo" : "dsave"), msg = fresh ? "nmsg" : "dmsg";
  btn.disabled = true;
  try {
    const d = await api("/api/admin/templates/type", fresh
      ? { new: true, key: val("key").toLowerCase(), title: val("title"), when: val("when"),
          base: document.getElementById("nbase").value, actions: document.getElementById("nactions").checked }
      : { new: false, key: tplOpen.id, title: val("title"), when: val("when"), actions: document.getElementById("dactions").checked });
    tpl = d;
    if (fresh) { tplNew = false; tplOpen = null; renderTemplates(); await openTemplate(d.key);
      say("tplmsg", "ok", "Тип добавлен. Ниже — его шаблон, копия выбранного: поправьте его под этот тип записей и сохраните."); }
    else { renderTemplates(); renderEditor(document.getElementById("tpltext").value); say("dmsg", "ok", "Описание сохранено."); }
    tick();
  } catch (err) { btn.disabled = false; say(msg, "err", err.message.charAt(0).toUpperCase() + err.message.slice(1)); }
});

/* ---------- настройки: оформление ----------
   Только корпоративный режим. Файл проверяется дважды: здесь сразу, чтобы не ждать ответа
   (вид файла по первым байтам, размер, стороны), и на сервере — всерьёз. Предпросмотр
   показывает меню в обеих темах и страницу входа ещё до загрузки. */
let brandData = null, brandPick = null, brandAsk = false, brandAdvice = [], brandSaid = null;

async function loadBrand() {
  try { brandData = await api("/api/admin/brand"); }
  catch (err) { document.getElementById("brandbody").innerHTML = `<div class="card"><div class="msg err">${esc(err.message)}</div></div>`; return; }
  renderBrand(true);
}

function brandPreview() {
  const d = brandData, typed = document.getElementById("bname");
  const name = (typed ? typed.value : d.name).trim().replace(/\s+/g, " ");
  const small = brandPick ? brandPick.url : d.public.logo ? d.public.logo.small : LOGO_SMALL;
  const full = brandPick ? brandPick.url : d.public.logo ? d.public.logo.full : LOGO;
  const mark = `<div class="pvbrand ${name ? "own" : ""}"><img src="${esc(small)}" alt="" height="36"><span>${esc(name || BRAND)}</span></div>`;
  const menu = (theme, title) => `<figure class="pv ${theme}"><div class="pvside">${mark}
      <i class="on"></i><i></i><i></i><i></i></div><figcaption>${title}</figcaption></figure>`;
  return `<div class="pvs">${menu("dark", "Меню, тёмная тема")}${menu("light", "Меню, светлая тема")}
    <figure class="pv light"><div class="pvlogin"><img src="${esc(full)}" alt=""><div class="pvbox"><b>Вход</b><i></i><i></i><u></u></div></div>
      <figcaption>Страница входа</figcaption></figure></div>
    <div class="fhint">${brandPick ? "Показан выбранный файл — он ещё не загружен." : "Так оформление выглядит сейчас."}
      ${name !== d.name ? " Название — из поля, оно ещё не сохранено." : ""} Значок вкладки браузера делается из того же логотипа.</div>`;
}

function renderBrand(fresh) {
  const d = brandData, box = document.getElementById("brandbody"), r = d.requirements;
  // набранное, но не сохранённое название переживает перерисовку раздела
  const was = document.getElementById("bname"), typed = was && !fresh ? was.value : d.name;
  const src = d.source || {}, own = !!(d.logo || d.name);
  box.innerHTML = `
    <div class="card">
      <h2>Логотип</h2>
      <p class="licwhy" style="margin-top:0">${d.logo
        ? `Загружен свой логотип: ${esc(src.format || "")}, ${src.width}×${src.height} точек, ${mb(src.bytes || 0)} — ${esc(d.by)}, ${stamp(d.at)}.`
        : "Сейчас показывается стандартный логотип сервиса."} Логотип видят все: в меню слева, на странице входа и значком вкладки.</p>
      <ul class="plain" id="brandreq">
        <li><b>Формат:</b> ${r.formats.join(", ").replace(/, ([^,]*)$/, " или $1")}. Сервис смотрит на содержимое файла, а не на расширение.</li>
        <li><b>SVG и GIF не принимаются.</b> ${esc(d.svg.split(": ").slice(1).join(": ").replace(/^./, c => c.toUpperCase()))}.</li>
        <li><b>Размер картинки:</b> меньшая сторона — не меньше ${r.min_side} точек, большая — не больше ${r.max_side}.</li>
        <li><b>Размер файла:</b> до ${Math.round(r.max_bytes / 1048576)} МБ.</li>
        <li><b>Лучше всего:</b> квадрат или близко к нему, прозрачный фон, без мелких надписей — в меню логотип показывается
          высотой около 36 точек. Это советы: такой файл сервис примет, но предупредит.</li>
      </ul>
      <label for="brandpick">Файл логотипа</label>
      <div class="row" style="margin-top:0"><button type="button" class="ghost" id="brandpick" aria-describedby="brandreq brandfile-err">${icon("image", 15)} Выбрать файл…</button>
        <span id="brandpicked" class="dim">${brandPick ? `${esc(brandPick.file.name)} · ${mb(brandPick.file.size)} · ${brandPick.w}×${brandPick.h}` : "файл не выбран"}</span></div>
      <input type="file" id="brandfile" accept="image/png,image/jpeg,image/webp" hidden>
      <div class="ferr" id="brandfile-err" role="alert"></div>
      <div class="row"><button id="brandgo" ${brandPick ? "" : "disabled"}>Загрузить логотип</button>
        ${brandPick ? `<button type="button" class="ghost" id="branddrop">Отмена</button>` : ""}</div>
      ${brandAdvice.length ? `<div class="edwarn" role="status"><b>${icon("alert", 14)} Логотип принят, но</b>
        <ul class="plain">${brandAdvice.map(a => `<li>${esc(a)}</li>`).join("")}</ul></div>` : ""}
      <div class="msg ${brandSaid ? brandSaid[0] : ""}" id="brandmsg" role="status">${brandSaid ? esc(brandSaid[1]) : ""}</div>
    </div>
    <div class="card">
      <h2>Название организации</h2>
      <form id="bnameform" novalidate>
        ${formField("bname", "Короткое название", `<input type="text" id="bname" maxlength="${r.name_max}" value="${esc(typed)}"
          autocomplete="organization" aria-describedby="bname-hint bname-err">`,
          `Необязательно, до ${r.name_max} знаков. Стоит рядом с логотипом вместо надписи «${BRAND}», в заголовке вкладки,
           в теме и шапке писем и в верхнем колонтитуле скачиваемых протоколов (HTML, DOCX, PDF). Пусто — надпись сервиса.`)}
        <div class="row"><button id="bnamego">Сохранить название</button><span class="dim" id="bnamecount"></span></div>
        <div class="msg" id="bnamemsg" role="status"></div>
      </form>
    </div>
    <div class="card">
      <h2>Как это выглядит</h2>
      <div id="brandpv">${brandPreview()}</div>
    </div>
    <div class="card">
      <h2>Стандартное оформление</h2>
      ${brandAsk ? `<div class="up" role="alertdialog" aria-labelledby="brandasktitle">
          <div class="uphead"><span class="upname" id="brandasktitle">Вернуть стандартное оформление?</span></div>
          <ul class="plain"><li>логотип организации и её название будут убраны: в меню, на странице входа и во вкладке
            снова будут логотип и надпись сервиса;</li><li>в письмах и скачиваемых файлах название организации пропадёт;</li>
            <li>загрузить свой логотип снова можно в любой момент.</li></ul>
          <div class="row"><button type="button" id="brandreset">Вернуть стандартный</button>
            <button type="button" class="ghost" id="brandkeep">Отмена</button></div></div>`
        : `<div class="row" style="margin-top:0"><button type="button" class="ghost" id="brandask" ${own ? "" : "disabled"}>${icon("undo", 15)} Вернуть стандартный</button>
            ${own ? "" : `<span class="dim">сейчас оформление стандартное</span>`}</div>`}
      ${d.state_kept ? "" : `<div class="nhint warn">${icon("alert", 14)}<span>Папка состояния не подключена с хоста: оформление
        пропадёт при пересоздании контейнера. Подключите том (см. install/README.md).</span></div>`}
    </div>`;
  const name = document.getElementById("bname");
  const count = () => { document.getElementById("bnamecount").textContent = `${name.value.trim().length} из ${r.name_max}`; };
  name.addEventListener("input", () => { count(); document.getElementById("brandpv").innerHTML = brandPreview(); });
  count();
  if (brandAsk) document.getElementById("brandkeep").focus();
}

/* Что за файл выбран — по первым байтам, как на сервере; стороны — как их видит браузер. */
async function brandCheck(file) {
  const r = brandData.requirements;
  if (file.size > r.max_bytes) return `Файл больше ${Math.round(r.max_bytes / 1048576)} МБ (${mb(file.size)}) — уменьшите картинку`;
  const head = new Uint8Array(await file.slice(0, 256).arrayBuffer());
  const ascii = String.fromCharCode(...head), text = ascii.replace(/^[\s﻿ï»¿]+/, "").toLowerCase();
  const png = ascii.startsWith("\u0089PNG"), jpeg = head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff;
  const webp = ascii.startsWith("RIFF") && ascii.slice(8, 12) === "WEBP";
  if (text.startsWith("<svg") || text.startsWith("<?xml")) return brandData.svg.charAt(0).toUpperCase() + brandData.svg.slice(1);
  if (ascii.startsWith("GIF8")) return "GIF не принимается: нужен PNG, JPEG или WebP. Сохраните логотип как PNG";
  if (!png && !jpeg && !webp) return "Это не картинка PNG, JPEG или WebP. Сервис смотрит на содержимое файла, а не на расширение";
  const url = URL.createObjectURL(file);
  const size = await new Promise(done => {
    const img = new Image();
    img.onload = () => done([img.naturalWidth, img.naturalHeight]);
    img.onerror = () => done(null);
    img.src = url;
  });
  if (!size) { URL.revokeObjectURL(url); return "Файл не читается как картинка: он повреждён или обрезан"; }
  const [w, h] = size;
  if (Math.min(w, h) < r.min_side) { URL.revokeObjectURL(url); return `Картинка слишком мала: ${w}×${h} точек, меньшая сторона нужна не меньше ${r.min_side}`; }
  if (Math.max(w, h) > r.max_side) { URL.revokeObjectURL(url); return `Картинка слишком велика: ${w}×${h} точек, сторона — не больше ${r.max_side}`; }
  return { file, url, w, h };
}

function brandForget() {
  if (brandPick) URL.revokeObjectURL(brandPick.url);
  brandPick = null;
}

const brandPane = document.querySelector('[data-pane="brand"]');
brandPane.addEventListener("change", async e => {
  if (e.target.id !== "brandfile" || !e.target.files.length) return;
  const seen = await brandCheck(e.target.files[0]);
  brandForget();
  brandAdvice = []; brandSaid = null;
  if (typeof seen === "string") { renderBrand(); fieldError2("brandfile-err", seen); document.getElementById("brandpick").focus(); return; }
  brandPick = seen;
  renderBrand();
  document.getElementById("brandgo").focus();
});

function fieldError2(boxId, text) {
  document.getElementById(boxId).innerHTML = text ? icon("alert", 14) + `<span>${esc(text)}</span>` : "";
}

brandPane.addEventListener("click", async e => {
  const hit = id => e.target.closest("#" + id);
  if (hit("brandpick")) document.getElementById("brandfile").click();
  if (hit("branddrop")) { brandForget(); brandSaid = null; renderBrand(); document.getElementById("brandpick").focus(); }
  if (hit("brandask")) { brandAsk = true; renderBrand(); }
  if (hit("brandkeep")) { brandAsk = false; renderBrand(); document.getElementById("brandask").focus(); }
  if (hit("brandgo") && brandPick) {
    const btn = hit("brandgo");
    btn.disabled = true; btn.textContent = "Загружаю…";
    try {
      const d = await putFile("/api/admin/brand/logo", brandPick.file);
      brandForget();
      brandData = d; brandAdvice = d.advice; brandSaid = ["ok", "Логотип загружен и уже показан в меню."];
      setBrand(d.public);
      renderBrand();
    } catch (err) {
      btn.disabled = false; btn.textContent = "Загрузить логотип";
      fieldError2("brandfile-err", err.message.charAt(0).toUpperCase() + err.message.slice(1));
    }
  }
  if (hit("brandreset")) {
    hit("brandreset").disabled = true;
    try {
      brandData = await api("/api/admin/brand/reset", {});
      brandForget();
      brandAsk = false; brandAdvice = []; brandSaid = ["ok", "Возвращено стандартное оформление."];
      setBrand(brandData.public);
      renderBrand(true);
    } catch (err) { brandAsk = false; brandSaid = ["err", err.message]; renderBrand(); }
  }
});

brandPane.addEventListener("submit", async e => {
  e.preventDefault();
  const field = document.getElementById("bname"), btn = document.getElementById("bnamego");
  fieldError("bname", "");
  btn.disabled = true;
  try {
    const d = await api("/api/admin/brand/name", { name: field.value });
    brandData = d;
    setBrand(d.public);
    renderBrand(true);
    say("bnamemsg", "ok", d.name ? `Название сохранено: ${d.name}.` : "Название убрано — стоит надпись сервиса.");
  } catch (err) {
    btn.disabled = false;
    fieldError("bname", err.message.charAt(0).toUpperCase() + err.message.slice(1));
    field.focus();
  }
});


/* ---------- папки и доступ ----------
   Личных папок здесь нет: у них один хозяин. Общую папку создаёт администратор сервиса,
   а дальше либо сам отмечает, кто её видит, либо назначает ей администратора. */
let folderList = null, personalList = [], everything = [], canCreate = false;
let folderOpen = null, folderUsers = [];        // folderOpen: путь открытой папки, "" — «все папки», null — закрыто
let folderAct = null;                           // действие над папкой: {kind: "sub" | "rename" | "delete", path}

async function loadFolders() {
  try {
    const d = await api("/api/admin/folders");
    folderList = d.folders; personalList = d.personal; everything = d.everything; canCreate = d.can_create;
    if (folderOpen && !folderList.some(f => f.path === folderOpen)) folderOpen = null;
    if (folderOpen !== null) folderUsers = (await api(`/api/access?dir=${encodeURIComponent(folderOpen)}`)).users;
  } catch (e) { return; }
  renderFolders();
}

function folderEditor() {
  if (folderOpen === null) return "";
  return `<div class="up" style="margin-top:14px">
    <div class="uphead"><span class="upname">${folderOpen ? `Доступ к папке «${esc(folderOpen)}»` : "Доступ ко всем папкам сразу"}</span>
      <span class="dim">${folderOpen ? "" : "включая личные папки других пользователей"}</span>
      <button class="ghost" id="fclose">Закрыть</button></div>
    ${accessTable(folderUsers, folderOpen, true)}
    <div class="msg" id="fmsg"></div></div>`;
}

/* Действие над папкой — подпапка, новое имя, удаление — открывается панелью под таблицей,
   как и доступ. Удаление разрушительно, поэтому оно отдельно, красным и с перечнем того,
   что именно уйдёт. */
function folderActPanel() {
  if (!folderAct) return "";
  const f = folderList.find(x => x.path === folderAct.path);
  if (!f) return "";
  const name = f.path.split("/").pop();
  const inside = folderList.filter(x => x.path.startsWith(f.path + "/")).length;
  const head = title => `<div class="uphead"><span class="upname">${title}</span>
      <button type="button" class="ghost" id="actclose">Отмена</button></div>`;
  if (folderAct.kind === "delete") return `<div class="up dangerbox" style="margin-top:14px" role="alertdialog" aria-labelledby="deltitle">
    <div class="uphead"><span class="upname" id="deltitle">Удалить папку «${esc(f.path)}»?</span></div>
    <ul class="plain">
      <li>записей в ней: <b>${f.records}</b>${inside ? `, подпапок: <b>${inside}</b>` : ""} — они исчезнут со страницы у всех;</li>
      <li>права, выданные на эту папку и подпапки, будут сняты;</li>
      <li>исходные файлы и результаты переедут в корзину на сервере и пролежат там 30 дней, потом сотрутся.
        Вернуть папку за это время можно только вручную на сервере.</li>
    </ul>
    <div class="row"><button type="button" class="danger" id="actdelete">Удалить папку${f.records ? ` и ${f.records} ${
      plural(f.records, "запись", "записи", "записей")}` : ""}</button>
      <button type="button" class="ghost" id="actclose">Отмена</button></div>
    <div class="msg" id="actmsg" role="status"></div></div>`;
  const sub = folderAct.kind === "sub";
  return `<form class="up" id="actform" style="margin-top:14px">
    ${head(sub ? `Новая подпапка в «${esc(f.path)}»` : `Новое имя для папки «${esc(f.path)}»`)}
    <label for="actname">${sub ? "Название подпапки" : "Название папки"}</label>
    <div class="row" style="margin-top:0">
      <input type="text" id="actname" autocomplete="off" style="flex:1 1 260px;max-width:420px"
             value="${sub ? "" : esc(name)}" required>
      <button>${sub ? "Создать подпапку" : "Переименовать"}</button></div>
    ${sub ? "" : `<div class="note" style="margin-top:10px">Вместе с папкой переедут её записи, результаты и выданные права.
      Ссылки на записи этой папки, которые кто-то сохранил, перестанут открываться.</div>`}
    <div class="msg" id="actmsg" role="status"></div></form>`;
}

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

function renderFolders() {
  const names = list => list.length ? list.map(esc).join(", ") : `<span class="dim">—</span>`;
  const picked = path => path === folderOpen || (folderAct && folderAct.path === path);
  document.getElementById("folders").innerHTML = (canCreate ? `
    <form class="row" id="fnew" style="margin-top:0">
      <input type="text" id="fname" autocomplete="off" style="flex:1 1 260px;max-width:420px"
             placeholder="Новая общая папка верхнего уровня" aria-label="Название новой общей папки" required>
      <button>Создать папку</button>
    </form>` : `<div class="note">Папка записей подключена только для чтения — создавать, переименовывать и удалять папки нельзя.</div>`) +
    `<div class="msg" id="fnewmsg"></div>` +
    `<div class="scroll"><table><thead><tr>
        <th>папка</th><th>чтение</th><th>загрузка</th><th>администрирование</th><th>записей</th><th>действия</th></tr></thead><tbody>
        <tr${folderOpen === "" ? ' class="picked"' : ""}>
          <td><span class="fcell">${icon("home", 14)}Все папки</span>
            <div class="dim" style="font-size:12px">право видеть всё, включая личные папки</div></td>
          <td>${names(everything)}</td><td colspan="2"><span class="dim">только администраторы сервиса</span></td><td class="num"></td>
          <td><div class="rowacts"><button class="ghost" data-folder="">Доступ</button></div></td></tr>` +
      folderList.map(f => {
        const depth = f.path.split("/").length - 1;
        const p = esc(f.path);
        return `<tr${picked(f.path) ? ' class="picked"' : ""}>
          <td style="padding-left:${depth * 18}px"><a class="fcell" href="/?path=${encodeURIComponent(f.path)}">${icon("folder", 14)}${esc(f.path.split("/").pop())}</a>
            ${depth ? `<div class="dim" style="font-size:12px">${p}</div>` : ""}</td>
          <td>${names(f.viewers)}</td><td>${names(f.uploaders)}</td><td>${names(f.managers)}</td><td class="num">${f.records}</td>
          <td><div class="rowacts"><button class="ghost" data-folder="${p}">Доступ</button>
            ${canCreate ? `<button class="ghost" data-act="sub" data-path="${p}">Подпапка</button>
            <button class="ghost" data-act="rename" data-path="${p}">Переименовать</button>
            <button class="ghost dangerlink" data-act="delete" data-path="${p}">Удалить</button>` : ""}</div></td></tr>`;
      }).join("") + `</tbody></table></div>` +
    (folderList.length ? "" : `<div class="empty">${icon("folder", 17)}общих папок пока нет — создайте первую</div>`) +
    folderEditor() + folderActPanel() +
    `<div class="note" style="margin-top:14px">В таблице — права, выданные именно на эту папку. Право «с подпапками»
      действует и на всё, что внутри; там оно показано как пришедшее сверху. Чтение загружать не разрешает:
      загрузка — отдельное право, потому что обработка каждой записи стоит денег.</div>`;
  const field = document.getElementById("actname");
  if (field && folderAct.fresh) { field.focus(); field.select(); folderAct.fresh = false; }
  document.getElementById("personal").innerHTML =
    (personalList.length ? `<div class="scroll"><table><thead><tr>
        <th>папка</th><th>владелец</th><th>загрузка</th><th>записей</th></tr></thead><tbody>` +
      personalList.map(p => `<tr>
        <td><a class="fcell" href="/?path=${encodeURIComponent(p.path)}">${icon("folder", 14)}${esc(p.path)}</a></td>
        <td>${esc(p.name)} <span class="dim">${esc(p.login)}</span></td>
        <td>${p.upload ? "разрешена" : `<span class="dim">нет</span>`}</td><td class="num">${p.records}</td></tr>`).join("") +
      `</tbody></table></div>`
      : `<div class="empty">${icon("folder", 17)}личная папка появляется у каждого нового пользователя</div>`) +
    `<div class="note" style="margin-top:10px">Личную папку видят только её владелец и администраторы сервиса; открыть её
      другим нельзя. Подпапки в ней владелец заводит сам. Загружать в неё записи он может, только если это разрешено
      в его учётной записи: «Пользователи» → «Изменить» → «Загрузка в личную папку».</div>`;
}

document.getElementById("folders").addEventListener("click", async e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.id === "fclose") { folderOpen = null; renderFolders(); }
  else if (b.id === "actclose") { folderAct = null; renderFolders(); }
  else if (b.dataset.act) { folderOpen = null; folderAct = { kind: b.dataset.act, path: b.dataset.path, fresh: true }; renderFolders(); }
  else if ("folder" in b.dataset) { folderAct = null; folderOpen = b.dataset.folder; loadFolders(); }
  else if (b.id === "actdelete") {
    b.disabled = true;
    say("actmsg", "", "Удаляю…");
    try {
      const done = await api("/api/admin/folders/delete", { dir: folderAct.path });
      const gone = folderAct.path;
      folderAct = null;
      await loadFolders();
      loadSidebar();
      say("fnewmsg", "ok", `Папка «${gone}» удалена. Содержимое лежит в корзине на сервере (${done.trash}) ${done.keep_days} дней.`);
    } catch (err) { b.disabled = false; say("actmsg", "err", err.message); }
  }
});
document.getElementById("folders").addEventListener("submit", async e => {
  e.preventDefault();
  try {
    if (e.target.id === "fnew") {
      folderOpen = (await api("/api/folder", { dir: document.getElementById("fname").value.trim() })).path;   // сразу к раздаче доступа
    } else {
      const name = document.getElementById("actname").value.trim();
      const made = folderAct.kind === "sub"
        ? await api("/api/folder", { dir: `${folderAct.path}/${name}` })
        : await api("/api/admin/folders/rename", { dir: folderAct.path, name });
      folderAct = null;
      folderOpen = made.path;               // новая или переименованная папка — сразу с её доступом
    }
    await loadFolders();
    loadSidebar();                          // меню слева показывает те же папки
  } catch (err) { say(e.target.id === "fnew" ? "fnewmsg" : "actmsg", "err", err.message); }
});
document.getElementById("folders").addEventListener("change", async e => {
  const select = e.target.closest("select[data-kind]");
  if (!select) return;
  try {
    await saveScope(select, folderOpen);
    await loadFolders();
    say("fmsg", "ok", "Сохранено");
  } catch (err) { say("fmsg", "err", err.message); await loadFolders(); }
});

/* ---------- расходы ----------
   Источник — журнал событий: строка на каждый прогон. Период считается по времени прогона,
   расход относится к папке записи и к человеку: кто запустил, а если сервис взял запись сам —
   кто её загрузил. Суммы — в валюте поставщика, который ответил: рубли у RouterAI, доллары
   у OpenRouter. Валюты не пересчитываются и не складываются: у каждой свой итог, своя колонка
   и свои графики, доля считается внутри валюты. */
const hours = s => (s / 3600).toFixed(1) + " ч";
const STAGES = { speech: "распознавание", triage: "разбор записи", actions: "поручения",
                 summary: "протокол", earlier: "до появления журнала" };
const PRESETS = [["today", "Сегодня", "day"], ["week", "Неделя", "day"], ["month", "Месяц", "day"],
                 ["year", "Год", "month"], ["all", "Всё время", "month"], ["custom", "Свои даты", null]];
const BUCKETS = [["day", "по дням"], ["week", "по неделям"], ["month", "по месяцам"], ["year", "по годам"]];
const GROUPS = [["folders", "Папки"], ["users", "Пользователи"], ["records", "Записи"], ["stages", "Этапы и модели"],
                ["providers", "Поставщики"]];
/* Виды графика по времени — у каждого свой вопрос. Столбики: когда тратили и на что (разбивка
   внутри периода). Линия: весь ход целиком, когда точек сотни. Нарастающим итогом: сколько
   набежало к дате. Цена часа: дорожает ли обработка и какой был объём. Календарь: в какие дни
   и недели была нагрузка. */
const VIEWS = [["bars", "Столбики"], ["line", "Линия"], ["cum", "Нарастающим итогом"],
               ["rate", "Цена часа и объём"], ["cal", "Календарь"]];
const SPLITS = [["", "без разбивки"], ["folders", "по папкам"], ["users", "по людям"], ["stages", "по этапам"]];
const MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
const WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];
const RECORDS_SHOWN = 100;
const TOP_SERIES = 5;           // рядов в стопке; остальное — «прочее»: больше цветов глаз не различит
const TOP_ROWS = 10;            // строк в диаграмме «на что ушло»
const CAL_MIN_DAYS = 14;        // календарь короче двух недель — одна строчка клеток
const PRINT_WIDTH = 680;        // ширина графика на листе A4
let cost = { preset: "month", bucket: "day", group: "folders", start: "", end: "", cur: "", view: "bars", split: "",
             data: null, prev: null };

const costPane = document.querySelector('[data-pane="costs"]');
/* Идёт печать: графики — под ширину листа, таблицы раскрыты, разрезы — все подряд. Страница
   перестраивается по событию beforeprint, до раскладки на листы, и возвращается по afterprint. */
let printing = false;

/* Выбранный вид живёт в адресе (/admin?period=year&view=cum#costs — ссылку можно отправить)
   и в памяти браузера: адрес важнее, без него берётся то, что смотрели в прошлый раз. */
const COST_KEY = "costs-view";
const COST_ADDRESS = { preset: "period", bucket: "step", view: "view", split: "split", group: "group", cur: "cur",
                       start: "from", end: "to" };
const COST_CHOICES = { preset: PRESETS, bucket: BUCKETS, view: VIEWS, split: SPLITS, group: GROUPS };

function costRecall() {
  const q = new URLSearchParams(location.search.length > 1 ? location.search : localStorage.getItem(COST_KEY) || "");
  for (const [field, name] of Object.entries(COST_ADDRESS)) {
    const v = q.get(name);
    if (v !== null && (COST_CHOICES[field] ? COST_CHOICES[field].some(c => c[0] === v) : /^[\w-]{1,10}$/.test(v)))
      cost[field] = v;
  }
}

function costRemember() {
  if (costPane.hidden) return;                  // пока грузилось, человек ушёл в другой раздел
  const q = new URLSearchParams();
  for (const [field, name] of Object.entries(COST_ADDRESS))
    if (cost[field] && (cost.preset === "custom" || (field !== "start" && field !== "end"))) q.set(name, cost[field]);
  localStorage.setItem(COST_KEY, q.toString());
  window.history.replaceState(null, "", `${location.pathname}?${q}#costs`);
}
// в других разделах вид отчёта в адресе ни к чему
addEventListener("hashchange", () => {
  if (location.hash !== "#costs" && location.search) window.history.replaceState(null, "", location.pathname + location.hash);
});

const isoDay = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const asDate = day => new Date(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10));
function presetRange(preset) {
  const now = new Date(), end = isoDay(now);
  if (preset === "today") return [end, end];
  if (preset === "week") { const d = new Date(now); d.setDate(d.getDate() - (d.getDay() + 6) % 7); return [isoDay(d), end]; }
  if (preset === "month") return [end.slice(0, 8) + "01", end];
  if (preset === "year") return [end.slice(0, 5) + "01-01", end];
  return ["", ""];
}

/* С чем сравнивать итоги: такой же отрезок прошлого месяца или года, прошлая неделя до того же
   дня недели, вчера; у своих дат — столько же дней сразу перед ними. У «всего времени» прошлого нет. */
function previousRange() {
  if (!cost.start || !cost.end) return null;
  const months = { month: 1, year: 12 }[cost.preset];
  const days = { today: 1, week: 7 }[cost.preset] || Math.round((asDate(cost.end) - asDate(cost.start)) / 864e5) + 1;
  return [cost.start, cost.end].map(day => {
    const d = asDate(day);
    if (!months) { d.setDate(d.getDate() - days); return isoDay(d); }
    const to = new Date(d.getFullYear(), d.getMonth() - months, 1);         // 31 марта → 28 февраля, а не 3 марта
    to.setDate(Math.min(d.getDate(), new Date(to.getFullYear(), to.getMonth() + 1, 0).getDate()));
    return isoDay(to);
  });
}

function periodOf(day, bucket) {
  if (bucket === "year") return day.slice(0, 4);
  if (bucket === "month") return day.slice(0, 7);
  const d = asDate(day);
  if (bucket === "week") d.setDate(d.getDate() - (d.getDay() + 6) % 7);
  return isoDay(d);
}
// подпись на оси — короткая; в подсказке и таблице — полная, с годом
const periodShort = (p, bucket, first) =>
  bucket === "year" ? p
  : bucket === "month" ? MONTHS[+p.slice(5, 7) - 1] + (first || p.slice(5, 7) === "01" ? " " + p.slice(0, 4) : "")
  : `${p.slice(8, 10)}.${p.slice(5, 7)}`;
const periodName = (p, bucket) =>
  bucket === "year" ? p + " год"
  : bucket === "month" ? `${MONTHS[+p.slice(5, 7) - 1]} ${p.slice(0, 4)}`
  : bucket === "week" ? `неделя с ${ruDay(p)}` : ruDay(p);

/* Ряд по времени без пропусков: день, когда ничего не тратили, — тоже точка на оси,
   иначе неделя тишины выглядит как соседние дни. */
function timeline() {
  const d = cost.data;
  if (!d.series.length) return [];
  const known = new Map(d.series.map(s => [s.period, s]));
  const last = d.end ? periodOf(d.end, d.bucket) : d.series.at(-1).period;
  const first = d.start ? periodOf(d.start, d.bucket) : d.series[0].period;
  const at = new Date(+first.slice(0, 4), (+first.slice(5, 7) || 1) - 1, +first.slice(8, 10) || 1);
  const key = () => periodOf(isoDay(at), d.bucket);
  const out = [];
  for (; key() <= last; d.bucket === "year" ? at.setFullYear(at.getFullYear() + 1)
                        : d.bucket === "month" ? at.setMonth(at.getMonth() + 1)
                        : at.setDate(at.getDate() + (d.bucket === "week" ? 7 : 1)))
    out.push(known.get(key()) || { period: key(), cost: {}, runs: 0, records: 0, audio_sec: 0 });
  return out;
}

async function loadCosts() {
  if (cost.preset !== "custom") [cost.start, cost.end] = presetRange(cost.preset);
  if (cost.view === "cal") cost.bucket = "day";             // календарь состоит из дней
  const before = previousRange();
  const ask = ([start, end]) => api(`/api/admin/report?start=${start}&end=${end}&bucket=${cost.bucket}`);
  try {
    [cost.data, cost.prev] = await Promise.all([ask([cost.start, cost.end]), before && ask(before)]);
    say("costmsg", "", "");
  } catch (err) { say("costmsg", "err", err.message); return; }
  renderCosts();
  loadActions();
}

/* ---------- графики ----------
   Без библиотек: SVG собирается строкой. У каждого графика одна остановка для Tab, дальше по
   точкам ходят стрелками; подсказка одна и та же по наведению и по фокусу, те же числа — в таблице
   под графиком. Цвет нигде не единственный носитель: рядом легенда с суммами и подписи. */
const axisNumber = new Intl.NumberFormat("ru", { maximumFractionDigits: 3 });
const px = v => Math.round(v * 10) / 10;

// «круглая» шкала от нуля: три–пять делений
function axisScale(max) {
  if (!(max > 0)) return { top: 1, ticks: [0] };
  const raw = max / 4, pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].find(m => m * pow >= raw) * pow;
  const count = Math.ceil(max / step - 1e-9);
  return { top: count * step, ticks: Array.from({ length: count + 1 }, (_, i) => i * step) };
}

// строки подсказки: [название, значение, цвет ряда]; say — то же самое словами для экранного диктора
const tipHtml = (title, rows) => `<b class="h">${esc(title)}</b>` + rows.map(([name, value, color]) =>
  `<div class="r">${color ? `<i style="background:${color}"></i>` : ""}<span>${esc(name)}</span><b>${esc(value)}</b></div>`).join("");
const tipSay = (title, rows) => `${title}: ${rows.map(([name, value]) => `${name} ${value}`).join(", ")}`;
const mark = (i, n, title, rows, inner) =>
  `<g class="pt" role="img" tabindex="${i === n - 1 ? 0 : -1}" aria-label="${esc(tipSay(title, rows))}" data-tip="${esc(tipHtml(title, rows))}">${inner}</g>`;

/* График по времени. o.kind: "bars" — столбики (o.colors и p.parts — стопкой), "line" — линия.
   Точка: {total, parts, short, title, rows, label}; total === null — в этом периоде значения нет.
   Столбики не уже 8 px: когда не помещаются, блок прокручивается вбок, а шкала слева стоит. */
function drawSeries(o, width) {
  const n = o.points.length, H = o.height || 220, top = 20, bottom = 22, axisW = 50;
  const room = Math.max(140, width - axisW);
  const bars = o.kind === "bars";
  const slot = bars && !printing ? Math.max(8, room / n) : room / n;
  const W = slot * n > room + .5 ? Math.ceil(slot * n) : room;
  const sc = axisScale(Math.max(0, ...o.points.map(p => p.total || 0), o.ref ? o.ref.value : 0));
  const plot = H - top - bottom, y = v => px(top + plot * (1 - v / sc.top)), y0 = y(0);
  const cx = i => px(slot * (i + .5));
  const colors = o.colors || ["var(--viz-1)"];

  const every = Math.max(1, Math.ceil(56 / slot)), centered = slot >= 56;
  const xlabels = o.points.map((p, i) => i % every || (!centered && i * slot + p.short.length * 7 > W) ? "" :
    `<text x="${centered ? cx(i) : px(i * slot + 1)}" y="${H - 6}"${centered ? ' text-anchor="middle"' : ""}>${esc(p.short)}</text>`).join("");

  let marks = "", under = "";
  if (bars) {
    const bw = Math.max(1, Math.min(28, slot - 2));
    const labelled = n <= 16 && slot >= 50;
    marks = o.points.map((p, i) => {
      const x = px(i * slot + (slot - bw) / 2), parts = (p.parts || [p.total]).map((v, k) => [v, colors[k]]).filter(([v]) => v > 0);
      const full = p.total > 0 ? Math.max(2, p.total / sc.top * plot) : 0;      // расход есть — столбик виден, каким бы малым он ни был
      let floor = y0, body = "";
      parts.forEach(([v, color], k) => {
        const h = v / p.total * full, last = k === parts.length - 1;
        const gap = k && h > 4 ? 2 : 0;                             // ряды в стопке разделяет просвет, а не обводка
        const up = px(floor - h), r = last ? Math.min(4, bw / 2, h) : 0;
        body += `<path style="fill:${color}" d="M${x},${px(floor - gap)}V${px(up + r)}Q${x},${up} ${px(x + r)},${up}H${px(x + bw - r)}Q${px(x + bw)},${up} ${px(x + bw)},${px(up + r)}V${px(floor - gap)}Z"/>`;
        floor -= h;
      });
      return mark(i, n, p.title, p.rows,
        `<rect class="band" x="${px(i * slot)}" y="1" width="${px(slot)}" height="${H - bottom - 1}"/>${body}` +
        (labelled && p.total > 0 ? `<text class="val" x="${cx(i)}" y="${px(y0 - full - 5)}" text-anchor="middle">${esc(p.label)}</text>` : ""));
    }).join("");
  } else {
    // линия рвётся там, где значения нет: соединять через пропуск — значит выдумывать ход
    const runs = [];
    o.points.forEach((p, i) => {
      if (p.total === null) return;
      if (i && o.points[i - 1].total !== null) runs.at(-1).push(i); else runs.push([i]);
    });
    const shown = o.points.filter(p => p.total !== null).length;
    under = runs.map(run => {
      const path = run.map((i, k) => `${k ? "L" : "M"}${cx(i)},${y(o.points[i].total)}`).join("");
      return (run.length > 1 ? `<path class="area" d="${path}V${y0}H${cx(run[0])}Z"/>` : "") + `<path class="line" d="${path}"/>`;
    }).join("");
    marks = o.points.map((p, i) => {
      const has = p.total !== null, lone = has && (o.points[i - 1] || {}).total == null && (o.points[i + 1] || {}).total == null;
      const left = cx(i) < W / 2;
      return mark(i, n, p.title, p.rows,
        `<rect class="band" x="${px(i * slot)}" y="1" width="${px(Math.max(slot, 1))}" height="${H - bottom - 1}"/>
         <line class="cross" x1="${cx(i)}" x2="${cx(i)}" y1="${top}" y2="${y0}"/>` +
        (has ? `<circle class="dot${shown <= 40 || lone ? "" : " far"}" cx="${cx(i)}" cy="${y(p.total)}" r="4"/>` : "") +
        (has && o.marks.includes(i) ? `<text class="val" x="${px(cx(i) + (left ? 8 : -8))}" y="${px(Math.max(12, y(p.total) - 8))}"${
          left ? "" : ' text-anchor="end"'}>${esc(p.label)}</text>` : ""));
    }).join("");
  }
  const grid = sc.ticks.map(t => `<line class="${t ? "grid" : "base"}" x1="0" x2="${W}" y1="${y(t)}" y2="${y(t)}"/>`).join("");
  const ref = o.ref ? `<line class="ref" x1="0" x2="${W}" y1="${y(o.ref.value)}" y2="${y(o.ref.value)}"/>
    <text class="val" x="4" y="${px(y(o.ref.value) - 5)}">${esc(o.ref.label)}</text>` : "";
  return `<div class="viz ${bars ? "vbars" : "vlines"}" role="group" aria-label="${esc(o.label)}. По точкам — стрелками влево и вправо; те же числа — в таблице ниже">
    <svg class="axis" width="${axisW}" height="${H}" aria-hidden="true">${sc.ticks.map(t =>
      `<text x="${axisW - 7}" y="${y(t) + 4}" text-anchor="end">${axisNumber.format(t)}</text>`).join("")}</svg>
    <div class="vizscroll"><svg width="${W}" height="${H}">${grid}${under}${ref}${marks}${xlabels}</svg></div>
    <div class="viztip" hidden aria-hidden="true"></div></div>`;
}

/* Календарь: неделя — столбец, день — клетка, чем больше потрачено, тем плотнее цвет (один тон,
   по светлоте — читается и без различения цветов). Уровни — по четвертям дней с расходом, а не
   долями от максимума: один дорогой день не делает все остальные одинаково бледными. */
function drawCalendar(days, cur, width) {
  const paid = days.map(s => s.cost[cur] || 0).filter(Boolean).sort((a, b) => a - b);
  const edges = [...new Set([.25, .5, .75, 1].map(q => paid[Math.max(0, Math.ceil(paid.length * q) - 1)]))];   // верхние границы уровней
  const level = v => v ? 5 - edges.length + edges.findIndex(e => v <= e) : 0;
  const lead = (asDate(days[0].period).getDay() + 6) % 7, weeks = Math.ceil((days.length + lead) / 7);
  const axisW = 28, top = 20;
  const pitch = printing ? Math.min(30, Math.floor((width - axisW) / weeks))
                                   : Math.max(14, Math.min(34, Math.floor((width - axisW) / weeks)));
  const cell = pitch - 3, numbered = pitch >= 26, H = top + 7 * pitch;
  const months = days.map((s, i) => ({ i, col: Math.floor((i + lead) / 7), day: s.period }))
    .filter(m => !m.i || m.day.endsWith("-01"));
  if (months.length > 1 && (months[1].col - months[0].col) * pitch < 58) months.shift();    // обрывок месяца в начале: подписи слиплись бы
  const cells = days.map((s, i) => {
    const v = s.cost[cur] || 0, lv = level(v), x = Math.floor((i + lead) / 7) * pitch, y0 = top + (i + lead) % 7 * pitch;
    const rows = [[`потрачено, ${sign(cur)}`, v ? amount(v, cur) : "—"], ["записей", s.records], ["звука", hours(s.audio_sec)]];
    return mark(i, days.length, `${WEEKDAYS[(i + lead) % 7]}, ${ruDay(s.period)}`, rows,
      `<rect class="cell l${lv}" x="${x + 1}" y="${y0 + 1}" width="${cell}" height="${cell}" rx="3"/>` +
      (numbered ? `<text class="day${lv > 2 ? " on" : ""}" x="${x + 1 + cell / 2}" y="${y0 + cell / 2 + 5}" text-anchor="middle">${+s.period.slice(8)}</text>` : ""));
  }).join("");
  return `<div class="viz cal" data-jump="7" role="group" aria-label="Календарь расходов в ${sign(cur)} по дням. Стрелки вверх и вниз — день, влево и вправо — неделя; те же числа — в таблице ниже">
    <svg class="axis" width="${axisW}" height="${H}" aria-hidden="true">${WEEKDAYS.map((w, r) =>
      pitch >= 22 || r % 2 === 0 ? `<text x="0" y="${top + r * pitch + cell / 2 + 5}">${w}</text>` : "").join("")}</svg>
    <div class="vizscroll"><svg width="${weeks * pitch}" height="${H}">${months.map((m, k) =>
      `<text x="${m.col * pitch + 1}" y="12">${MONTHS[+m.day.slice(5, 7) - 1]}${!k || m.day.slice(5, 7) === "01" ? " " + m.day.slice(0, 4) : ""}</text>`).join("")}${cells}</svg></div>
    <div class="viztip" hidden aria-hidden="true"></div></div>
    <ul class="legend" aria-label="Шкала календаря">
      <li><i class="l0"></i>нет расходов</li>${edges.map((e, k) =>
        `<li><i style="background:var(--heat-${5 - edges.length + k})"></i>${k ? "до" : "день до"} ${amount(e, cur)}</li>`).join("")}</ul>`;
}

function showTip(pt) {
  const viz = pt.closest(".viz"), tip = viz.querySelector(".viztip");
  tip.innerHTML = pt.dataset.tip;
  tip.hidden = false;
  const at = pt.getBoundingClientRect(), box = viz.getBoundingClientRect();
  let left = at.right - box.left + 8;
  if (left + tip.offsetWidth > box.width) left = at.left - box.left - 8 - tip.offsetWidth;
  tip.style.left = Math.max(0, Math.min(left, box.width - tip.offsetWidth)) + "px";
  // у календаря подсказка над клеткой или под ней — саму клетку не закрывает
  const above = at.top - box.top - tip.offsetHeight - 6;
  tip.style.top = (viz.classList.contains("cal") ? (above < -40 ? at.bottom - box.top + 6 : above) : 0) + "px";
}
const hideTips = () => costPane.querySelectorAll(".viztip").forEach(tip => { tip.hidden = true; });

costPane.addEventListener("pointerover", e => { const pt = e.target.closest(".pt"); if (pt) showTip(pt); });
costPane.addEventListener("pointerout", e => {
  // палец «уходит» сразу после касания — подсказка остаётся до следующего касания
  if (e.pointerType !== "touch" && e.target.closest(".viz") && !(e.relatedTarget && e.relatedTarget.closest(".viz"))) hideTips();
});
costPane.addEventListener("focusin", e => { const pt = e.target.closest(".pt"); if (pt) showTip(pt); });
costPane.addEventListener("focusout", e => { if (e.target.closest(".pt")) hideTips(); });
costPane.addEventListener("keydown", e => {
  const pt = e.target.closest(".pt");
  if (!pt) return;
  if (e.key === "Escape") { hideTips(); return; }
  const all = [...pt.closest(".viz").querySelectorAll(".pt")], at = all.indexOf(pt);
  const jump = +pt.closest(".viz").dataset.jump || 0;       // календарь: влево-вправо — на неделю
  const to = { ArrowLeft: at - (jump || 1), ArrowRight: at + (jump || 1), ArrowUp: at - 1, ArrowDown: at + 1,
               Home: 0, End: all.length - 1 }[e.key];
  if (to === undefined || (!jump && (e.key === "ArrowUp" || e.key === "ArrowDown"))) return;
  e.preventDefault();
  const next = all[Math.max(0, Math.min(all.length - 1, to))];
  pt.setAttribute("tabindex", "-1");
  next.setAttribute("tabindex", "0");
  next.focus();
});

const legend = series => `<ul class="legend">${series.map(s =>
  `<li><i style="background:${s.color}"></i>${esc(s.name)} <b>${esc(s.value)}</b></li>`).join("")}</ul>`;
const sheet = (head, rows) => `<details class="plain"${printing ? " open" : ""}><summary>Таблицей</summary>
  <div class="scroll"><table><thead><tr>${head.map((h, i) => `<th${i ? ' class="num"' : ""}>${h}</th>`).join("")}</tr></thead><tbody>${
    rows.map(row => `<tr>${row.map((c, i) => `<td${i ? ' class="num r"' : ""}>${c}</td>`).join("")}</tr>`).join("")
  }</tbody></table></div></details>`;
const dash = `<span class="dim">—</span>`;

/* Столбик по разрезу: пять самых дорогих за весь период и «прочее». У этапов порядок и цвет
   постоянные — распознавание всегда первое и синее, какой бы период ни смотрели. */
function splitSeries(line, kind, cur) {
  const d = cost.data, cells = d.split[kind], sums = new Map(), stages = Object.keys(STAGES);
  for (const s of line)
    for (const [key, sum] of Object.entries(cells[s.period] || {}))
      if (sum[cur]) sums.set(key, (sums.get(key) || 0) + sum[cur]);
  const order = kind === "stages" ? stages.filter(k => sums.has(k))
    : [...sums.keys()].sort((a, b) => sums.get(b) - sums.get(a)).slice(0, TOP_SERIES);
  const name = key => kind === "stages" ? STAGES[key]
    : kind === "users" ? (key ? d.names[key] || key : "неизвестно") : key || "вне папок";
  const series = order.map((key, i) => ({ name: name(key), total: sums.get(key),
                                          color: `var(--viz-${(kind === "stages" ? stages.indexOf(key) : i) + 1})` }));
  const others = [...sums.keys()].filter(k => !order.includes(k));
  if (others.length) series.push({ name: "прочее", color: "var(--viz-rest)", total: others.reduce((a, k) => a + sums.get(k), 0) });
  const rows = line.map(s => {
    const cell = cells[s.period] || {}, of = k => (cell[k] || {})[cur] || 0;
    return [...order.map(of), ...(others.length ? [others.reduce((a, k) => a + of(k), 0)] : [])];
  });
  return { series, rows };
}

function renderSeries() {
  const d = cost.data, cur = cost.cur, curs = d.currencies, line = timeline();
  const box = document.getElementById("costseries"), bar = document.getElementById("costview");
  const of = (sum, c) => sum[c] || 0, paid = v => v ? amount(v, cur) : dash;
  const step = BUCKETS.find(b => b[0] === d.bucket)[1], unit = sign(cur);
  const width = printing ? PRINT_WIDTH : box.clientWidth;
  const days = d.start && d.end ? Math.round((asDate(d.end) - asDate(d.start)) / 864e5) + 1
    : line.length * { day: 1, week: 7, month: 30, year: 365 }[d.bucket];
  const calendar = days >= CAL_MIN_DAYS;
  const view = cost.view === "cal" && !calendar ? "bars" : cost.view;
  bar.hidden = !line.length || !cur;
  if (bar.hidden) { box.innerHTML = `<div class="empty">${icon("idle", 17)}за этот период расходов не было</div>`; return; }
  bar.innerHTML = `
    <div><label id="viewlabel">Вид</label>
      <div class="tabs" role="group" aria-labelledby="viewlabel">${VIEWS.filter(v => v[0] !== "cal" || calendar).map(([id, label]) =>
        `<button type="button" data-view="${id}" aria-pressed="${view === id}" aria-selected="${view === id}">${label}</button>`).join("")}</div></div>
    ${view === "bars" ? `<div><label for="costsplit">Внутри столбика</label><select id="costsplit">${SPLITS.map(([id, label]) =>
      `<option value="${id}"${cost.split === id ? " selected" : ""}>${label}</option>`).join("")}</select></div>` : ""}`;

  const point = (s, i, total, rows) => ({ total, rows, label: total ? amount(total, cur) : "", title: periodName(s.period, d.bucket),
                                          short: periodShort(s.period, d.bucket, !i) });
  const volume = s => [["записей", s.records], ["звука", hours(s.audio_sec)], ["прогонов", s.runs]];
  const spent = s => [...curs.map(c => [`потрачено, ${sign(c)}`, of(s.cost, c) ? amount(of(s.cost, c), c) : "—"]), ...volume(s)];
  const plain = () => sheet(["период", ...curs.map(c => `потрачено, ${sign(c)}`), "записей", "звука", "прогонов"],
    line.map(s => [periodName(s.period, d.bucket), ...curs.map(c => of(s.cost, c) ? amount(of(s.cost, c), c) : dash),
                   s.records, hours(s.audio_sec), s.runs]));
  const title = text => `<h3 class="viztitle">${text}</h3>`;
  let html;

  if (view === "bars" && cost.split) {
    const { series, rows } = splitSeries(line, cost.split, cur), sum = row => row.reduce((a, v) => a + v, 0);
    const what = SPLITS.find(s => s[0] === cost.split)[1];
    html = title(`Расходы ${step} ${what}, ${unit}`) +
      legend(series.map(s => ({ ...s, value: amount(s.total, cur) }))) +
      drawSeries({ kind: "bars", label: `Расходы в ${unit} ${step} ${what}`, colors: series.map(s => s.color),
        points: line.map((s, i) => ({ ...point(s, i, sum(rows[i]), [
          ...series.map((ser, k) => [ser.name, rows[i][k] ? amount(rows[i][k], cur) : "—", ser.color]).filter(r => r[1] !== "—"),
          [`всего, ${unit}`, sum(rows[i]) ? amount(sum(rows[i]), cur) : "—"], ...volume(s)]), parts: rows[i] })) }, width) +
      (series.at(-1).name === "прочее" ? `<div class="note" style="margin-top:12px">Показаны ${TOP_SERIES} самых дорогих за период, остальные сложены в «прочее». Полный список — в таблице разреза ниже.</div>` : "") +
      sheet(["период", ...series.map(s => esc(s.name)), `всего, ${unit}`],
        line.map((s, i) => [periodName(s.period, d.bucket), ...rows[i].map(paid), paid(sum(rows[i]))]));
  } else if (view === "bars" || view === "line") {
    const points = line.map((s, i) => point(s, i, of(s.cost, cur), spent(s)));
    const peak = points.reduce((best, p, i) => p.total > points[best].total ? i : best, 0);
    html = title(`Расходы ${step}, ${unit}`) +
      drawSeries({ kind: view, label: `Расходы в ${unit} ${step}`, points, marks: [peak] }, width) + plain();
  } else if (view === "cum") {
    let run = 0;
    const totals = line.map(s => run += of(s.cost, cur));
    html = title(`Расходы нарастающим итогом, ${unit}`) +
      drawSeries({ kind: "line", label: `Расходы в ${unit} нарастающим итогом ${step}`, marks: [line.length - 1],
        points: line.map((s, i) => point(s, i, totals[i], [[`с начала периода, ${unit}`, amount(totals[i], cur)],
                                                             [`за период, ${unit}`, of(s.cost, cur) ? amount(of(s.cost, cur), cur) : "—"]])) }, width) +
      sheet(["период", `за период, ${unit}`, `нарастающим итогом, ${unit}`],
        line.map((s, i) => [periodName(s.period, d.bucket), paid(of(s.cost, cur)), amount(totals[i], cur)]));
  } else if (view === "rate") {
    // цена часа — внутри валюты: расход в ней на звук записей, за которые в ней платили
    const heard = s => of(d.paid_audio.series[s.period] || {}, cur), rate = s => heard(s) ? of(s.cost, cur) / (heard(s) / 3600) : null;
    const all = of(d.paid_audio.total, cur), mean = all ? of(d.total.cost, cur) / (all / 3600) : 0;
    const small = Math.max(220, printing || width < 620 ? width : (width - 24) / 2);
    const bars = (label, value, show) => drawSeries({ kind: "bars", label: `${label} ${step}`, height: 150,
      points: line.map((s, i) => ({ ...point(s, i, value(s), volume(s)), label: value(s) ? show(value(s)) : "" })) }, small);
    html = title(`Цена часа звука ${step}, ${unit}`) +
      drawSeries({ kind: "line", label: `Цена часа звука в ${unit} ${step}`, marks: [],
        ref: mean ? { value: mean, label: `в среднем за период ${amount(mean, cur)}` } : null,
        points: line.map((s, i) => point(s, i, rate(s), [[`за час звука, ${unit}`, rate(s) === null ? "—" : amount(rate(s), cur)],
          [`потрачено, ${unit}`, of(s.cost, cur) ? amount(of(s.cost, cur), cur) : "—"], ["оплачено звука", hours(heard(s))]])) }, width) +
      `<div class="vizpair"><div>${title("Часов звука")}${bars("Часов звука", s => s.audio_sec / 3600, v => v.toFixed(1).replace(".", ","))}</div>
        <div>${title("Записей")}${bars("Записей", s => s.records, String)}</div></div>
      <div class="note" style="margin-top:12px">Цена часа — расход в ${unit}, делённый на часы звука тех записей, за которые платили в ${unit}.
        Пересборка протокола прибавляет деньги, но не часы: период, где записи только пересобирали, выглядит дороже.</div>` +
      sheet(["период", `за час звука, ${unit}`, `потрачено, ${unit}`, `оплачено звука`, "всего звука", "записей"],
        line.map(s => [periodName(s.period, d.bucket), rate(s) === null ? dash : amount(rate(s), cur), paid(of(s.cost, cur)),
                       hours(heard(s)), hours(s.audio_sec), s.records]));
  } else {
    html = title(`Расходы по дням, ${unit}`) + drawCalendar(line, cur, width) + plain();
  }
  box.innerHTML = html;
  box.dataset.width = width;
  box.querySelectorAll(".vizscroll").forEach(el => { el.scrollLeft = el.scrollWidth; });      // свежее — справа, с него и начинаем
}

/* «На что ушло» — те же строки, что в таблице разреза, но по убыванию и в одной валюте:
   первые десять столбиками, остаток одной строкой без столбика (сумма сотен записей была бы
   длиннее любого из них и сжала бы остальные). У этапов сверху — полоса долей. */
function costChart(group) {
  const d = cost.data, cur = cost.cur, total = d.total.cost[cur] || 0;
  if (!total) return "";
  const of = sum => sum[cur] || 0, unit = sign(cur);
  const share = v => `${v / total < .01 ? "<1" : Math.round(v / total * 100)} %`;
  const [what, rows] = {
    folders: () => ["Папки: потрачено на записи самой папки, без подпапок",
                    [...d.folders.map(f => [f.folder, of(f.own)]),
                     ["вне папок", Math.round((total - d.folders.reduce((a, f) => a + of(f.own), 0)) * 1e6) / 1e6]]],
    users: () => ["Кто потратил", d.users.map(u => [u.login ? d.names[u.login] || u.login : "неизвестно", of(u.cost)])],
    records: () => ["Самые дорогие записи", d.records.map(r => [r.file.split("/").pop(), of(r.cost), r.file])],
    stages: () => ["Этапы и модели", d.stages.map(s => [(STAGES[s.stage] || s.stage) + (s.model ? ` · ${s.model}` : ""), of(s.cost)])],
    providers: () => ["Кому заплачено", d.providers.map(p => [p.provider || "не назван", of(p.cost)])],
  }[group]();
  const sorted = rows.filter(r => r[1] > 0).sort((a, b) => b[1] - a[1]), shown = sorted.slice(0, TOP_ROWS);
  const tail = sorted.slice(TOP_ROWS), top = shown.length ? shown[0][1] : 0;
  const row = ([name, v, full], cls) => `<li${cls ? ` class="${cls}"` : ""}><span class="hname" title="${esc(full || name)}">${esc(name)}</span>
    <span class="htrack">${cls ? "" : `<i style="width:${px(v / top * 100)}%"></i>`}</span>
    <span class="hval">${amount(v, cur)} <span class="dim">${share(v)}</span></span></li>`;

  let strip = "";
  if (group === "stages") {
    const sums = new Map();
    d.stages.forEach(s => of(s.cost) && sums.set(s.stage, (sums.get(s.stage) || 0) + of(s.cost)));
    const parts = Object.keys(STAGES).filter(k => sums.has(k)).map(k =>
      ({ name: STAGES[k], v: sums.get(k), color: `var(--viz-${Object.keys(STAGES).indexOf(k) + 1})` }));
    strip = `<h3 class="viztitle">Доли этапов, ${unit}</h3>
      <div class="strip" role="img" aria-label="Доли этапов в расходах в ${unit}: ${esc(parts.map(p => `${p.name} ${share(p.v)}`).join(", "))}">${
        parts.map(p => `<i style="flex-grow:${p.v};background:${p.color}" title="${esc(p.name)}: ${amount(p.v, cur)}, ${share(p.v)}"></i>`).join("")}</div>` +
      legend(parts.map(p => ({ ...p, value: `${amount(p.v, cur)} · ${share(p.v)}` })));
  }
  return strip + `<h3 class="viztitle">${what}, ${unit}</h3>
    <ol class="hbars" aria-label="${esc(what)}, ${unit}: от дорогих к дешёвым">${shown.map(r => row(r)).join("")}${
      tail.length ? row([`ещё ${tail.length}`, tail.reduce((a, r) => a + r[1], 0)], "rest") : ""}</ol>`;
}

function renderCosts() {
  const d = cost.data, t = d.total, p = cost.prev;
  // страница перерисовывается целиком: нажатая с клавиатуры кнопка не должна терять фокус
  const held = document.activeElement, again = costPane.contains(held) && (held.id ? "#" + held.id
    : ["preset", "cur", "view", "group"].map(k => held.dataset[k] && `[data-${k}="${held.dataset[k]}"]`).find(Boolean));
  const curs = d.currencies;                    // валюты, в которых за период что-то потрачено
  if (!curs.includes(cost.cur)) cost.cur = curs[0] || "";
  const curName = cur => `${cur === "RUB" ? "Рубли" : cur === "USD" ? "Доллары" : esc(cur)}, ${sign(cur)}`;
  // файл — весь отчёт по тому, что выбрано на странице: период и шаг
  const file = new URLSearchParams({ start: cost.start, end: cost.end, bucket: cost.bucket });
  // Рубли и доллары на одной оси сравнивать нельзя: графики показывают одну валюту, вторая — переключателем.
  document.getElementById("costbar").innerHTML = `
    <div><label id="presetlabel">Период</label>
      <div class="tabs" role="group" aria-labelledby="presetlabel">${PRESETS.map(([id, label]) =>
        `<button type="button" data-preset="${id}" aria-pressed="${cost.preset === id}"
                 aria-selected="${cost.preset === id}">${label}</button>`).join("")}</div></div>
    <div><label for="coststart">С</label><input type="date" id="coststart" value="${cost.start}"></div>
    <div><label for="costend">По</label><input type="date" id="costend" value="${cost.end}"></div>
    <div><label for="costbucket">Шаг графика</label><select id="costbucket">${BUCKETS.map(([id, label]) =>
      `<option value="${id}"${cost.bucket === id ? " selected" : ""}>${label}</option>`).join("")}</select></div>
    ${curs.length > 1 ? `<div><label id="curlabel">Валюта графиков</label>
      <div class="tabs" id="costcur" role="group" aria-labelledby="curlabel">${curs.map(cur =>
        `<button type="button" data-cur="${cur}" aria-pressed="${cost.cur === cur}" aria-selected="${cost.cur === cur}">${curName(cur)}</button>`).join("")}</div></div>` : ""}
    <span class="grow"></span>
    <div><label id="filelabel">Скачать отчёт целиком</label>
      <div class="files" role="group" aria-labelledby="filelabel">
        <a class="navlink" id="costxlsx" href="/api/admin/report.xlsx?${file}" download
           title="Книга Excel: сводка и по листу на каждый разрез — за выбранный период">${icon("copy", 15)}Excel</a>
        <a class="navlink" id="costcsv" href="/api/admin/report.csv?${file}" download
           title="Один файл CSV: те же таблицы подряд — за выбранный период">${icon("copy", 15)}CSV</a>
        <button type="button" class="ghost" id="costprint"
           title="Печать всего отчёта: все разрезы подряд; в окне печати выберите «Сохранить как PDF»">${icon("doc", 15)}PDF (печать)</button>
      </div></div>`;

  const now = new Date();
  document.getElementById("costprinted").textContent =
    `Подьячий, расходы. Период: ${cost.start ? ruDay(cost.start) : "с начала"} — ${cost.end ? ruDay(cost.end) : "по сегодня"}, ` +
    `шаг ${BUCKETS.find(b => b[0] === cost.bucket)[1]}. ` +
    (cost.cur ? `Графики — в ${sign(cost.cur)}${curs.length > 1 ? "; рубли и доллары не пересчитываются и не складываются" : ""}. ` : "") +
    `Отчёт сформирован ${ruDay(isoDay(now))} в ${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}` +
    `${me && me.login ? `, ${me.name || me.login}` : ""}.`;

  /* Плитка сравнивает с прошлым таким же отрезком: стрелка и процент словами, без «хорошо/плохо» —
     рост расходов сам по себе ни то ни другое. Не с чем сравнить — так и сказано. */
  const delta = (value, was) => {
    if (!p) return "";
    if (!was) return `<div class="delta">было: нет данных</div>`;
    const part = (value - was) / was;
    const text = Math.abs(part) < .005 ? "без изменений"
      : part >= 9 ? `в ${Math.round(value / was)} ${plural(Math.round(value / was), "раз", "раза", "раз")} больше`
      : `${part > 0 ? "+" : "−"}${Math.round(Math.abs(part) * 100)} %`;
    return `<div class="delta"><span aria-hidden="true">${Math.abs(part) < .005 ? "=" : part > 0 ? "▲" : "▼"}</span> ${text}</div>`;
  };
  const perHour = (r, cur) => r.paid_audio.total[cur] ? r.total.cost[cur] / (r.paid_audio.total[cur] / 3600) : 0;
  document.getElementById("costtiles").innerHTML = [
    ...(curs.length ? curs.map(cur => [`потрачено, ${sign(cur)}`, amount(t.cost[cur], cur), "coin", t.cost[cur], p && p.total.cost[cur]])
                    : [["потрачено", "—", "coin"]]),
    ["записей", t.records, "list", t.records, p && p.total.records],
    ["звука", hours(t.audio_sec), "mic", t.audio_sec, p && p.total.audio_sec],
    ["прогонов", t.runs, "clock", t.runs, p && p.total.runs],
    // цена часа — внутри валюты: расход в ней на звук записей, за которые в ней платили
    ...curs.filter(cur => perHour(d, cur)).map(cur =>
      [`за час звука, ${sign(cur)}`, amount(perHour(d, cur), cur), "coin", perHour(d, cur), p && perHour(p, cur)]),
  ].map(([l, n, ic, value, was]) => `<div class="tile"><div class="top">${icon(ic, 14)}<span class="l">${l}</span></div>
      <div class="n">${esc(n)}</div>${value ? delta(value, was) : ""}</div>`).join("");
  const earlier = p && (p.start === p.end ? ruDay(p.start) : `${ruDay(p.start)} — ${ruDay(p.end)}`);
  document.getElementById("costcompare").textContent = !p ? ""
    : p.total.runs ? `Под числом — изменение к такому же отрезку раньше: ${earlier}.`
    : `За такой же отрезок раньше (${earlier}) в журнале расходов нет — сравнивать не с чем.`;

  renderSeries();
  document.getElementById("costgroup").innerHTML = GROUPS.map(([id, label]) =>
    `<button type="button" role="tab" data-group="${id}" aria-selected="${cost.group === id}">${label}</button>`).join("");
  // на странице — один разрез, выбранный вкладкой; на печати — все подряд, у каждого свой заголовок
  document.getElementById("costchart").innerHTML = printing ? "" : costChart(cost.group);
  document.getElementById("costtable").innerHTML = !printing || !t.runs ? costTable(cost.group)
    : GROUPS.map(([id, label]) => `<section class="printgroup"><h3>${label}</h3>${costChart(id)}${costTable(id)}</section>`).join("");
  if (again && costPane.querySelector(again)) costPane.querySelector(again).focus();
  costRemember();
}

function costTable(group) {
  const d = cost.data, total = d.total.cost, curs = d.currencies;
  const empty = `<div class="empty">${icon("idle", 17)}за этот период расходов не было</div>`;
  if (!d.total.runs) return empty;
  // Колонка на валюту: сумма и под ней полоска — доля строки в итоге этой же валюты.
  // sub — потрачено на записи самой папки, без подпапок.
  const cells = (sum, sub) => curs.map(cur => {
    const v = sum[cur] || 0, part = total[cur] ? Math.round(v / total[cur] * 100) : 0;
    if (!v) return `<td class="num r curcell"><span class="dim">—</span></td>`;
    return `<td class="num r curcell"><b>${amount(v, cur)}</b>
      <div class="bar" role="img" aria-label="${part}% от итога в ${sign(cur)}" title="${part}% от итога в ${sign(cur)}"><i style="width:${v / total[cur] * 100}%"></i></div>
      ${sub && (sub[cur] || 0) !== v ? `<div class="dim sub">без подпапок ${sub[cur] ? amount(sub[cur], cur) : "—"}</div>` : ""}</td>`;
  }).join("");
  const spentHead = curs.map(cur => `<th class="num">потрачено, ${sign(cur)}</th>`).join("");
  const nums = r => `<td class="num r">${r.records}</td><td class="num r">${hours(r.audio_sec)}</td><td class="num r">${r.runs}</td>`;
  const head = first => `<div class="scroll" style="margin-top:12px"><table><thead><tr><th>${first}</th>${spentHead}
    <th class="num">записей</th><th class="num">звука</th><th class="num">прогонов</th></tr></thead><tbody>`;
  const two = curs.length > 1 ? ` Рубли и доллары не пересчитываются и не складываются: полоска под суммой — доля в итоге своей валюты.` : "";
  const note = text => `<div class="note" style="margin-top:12px">${text}${two}</div>`;

  if (group === "folders") return head("папка") + d.folders.map(f => {
    const depth = f.folder.split("/").length - 1;
    return `<tr><td style="padding-left:${depth * 18}px"><span class="fcell">${icon("folder", 14)}${esc(f.folder.split("/").pop())}</span></td>
      ${cells(f.cost, f.own)}${nums(f)}</tr>`;
  }).join("") + `</tbody></table></div>` +
    note("«Потрачено» у папки — вместе с подпапками. Записи вне папок входят только в общий итог.");

  if (group === "users") return head("кто потратил") + d.users.map(u =>
    `<tr><td>${u.login ? `${esc(d.names[u.login] || u.login)} <span class="dim">${esc(u.login)}</span>`
                       : `<span class="dim">неизвестно — до журнала или мимо страницы</span>`}</td>
      ${cells(u.cost)}${nums(u)}</tr>`).join("") + `</tbody></table></div>` +
    note("Расход относится к тому, кто запустил обработку или пересборку; если сервис взял запись сам — к тому, кто её загрузил.");

  if (group === "providers") return head("поставщик") + d.providers.map(p =>
    `<tr><td>${p.provider ? `<b>${esc(p.provider)}</b>` : `<span class="dim">не назван</span>`}</td>
      ${cells(p.cost)}${nums(p)}</tr>`).join("") + `</tbody></table></div>` +
    note("Кому заплачено. Запись, которую начал основной поставщик, а закончил запасной, посчитана у обоих — " +
         "каждому своя часть. Расходы до появления второго поставщика отнесены к OpenRouter.");

  if (group === "records") return `<div class="scroll" style="margin-top:12px"><table><thead><tr><th>запись</th>${spentHead}
      <th class="num">звука</th><th class="num">прогонов</th></tr></thead><tbody>` +
    d.records.slice(0, RECORDS_SHOWN).map(r => {
      const cut = r.file.lastIndexOf("/");
      return `<tr><td>${esc(r.file.slice(cut + 1))}<div class="dim" style="font-size:12px">${esc(r.file.slice(0, Math.max(cut, 0)))}</div></td>
        ${cells(r.cost)}<td class="num r">${hours(r.audio_sec)}</td><td class="num r">${r.runs}</td></tr>`;
    }).join("") + `</tbody></table></div>` + (d.records.length > RECORDS_SHOWN
      ? `<div class="note" style="margin-top:12px">Показаны ${RECORDS_SHOWN} самых дорогих из ${d.records.length}. Все — в файле Excel или CSV.</div>` : "");

  return `<div class="scroll" style="margin-top:12px"><table><thead><tr><th>этап</th><th>модель</th>${spentHead}</tr></thead><tbody>` +
    d.stages.map(s => `<tr><td>${esc(STAGES[s.stage] || s.stage)}</td><td>${s.model ? esc(s.model) : `<span class="dim">—</span>`}</td>
      ${cells(s.cost)}</tr>`).join("") + `</tbody></table></div>`;
}

document.getElementById("costbar").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.id === "costprint") print();
  else if (b.dataset.cur) { cost.cur = b.dataset.cur; renderCosts(); }
  else if (b.dataset.preset) {
    cost.preset = b.dataset.preset;
    const step = PRESETS.find(p => p[0] === cost.preset)[2];
    if (step) cost.bucket = step;
    loadCosts();
  }
});
document.getElementById("costbar").addEventListener("change", e => {
  if (e.target.id === "costbucket") {
    cost.bucket = e.target.value;
    if (cost.view === "cal" && cost.bucket !== "day") cost.view = "bars";      // календарь бывает только по дням
  }
  else if (e.target.id === "coststart") { cost.start = e.target.value; cost.preset = "custom"; }
  else if (e.target.id === "costend") { cost.end = e.target.value; cost.preset = "custom"; }
  else return;
  loadCosts();
});
document.getElementById("costview").addEventListener("click", e => {
  const b = e.target.closest("button[data-view]");
  if (!b) return;
  cost.view = b.dataset.view;
  if (cost.view === "cal" && cost.bucket !== "day") loadCosts(); else renderCosts();
});
document.getElementById("costview").addEventListener("change", e => {
  if (e.target.id === "costsplit") { cost.split = e.target.value; renderCosts(); }
});
document.getElementById("costgroup").addEventListener("click", e => {
  const b = e.target.closest("button[data-group]");
  if (b) { cost.group = b.dataset.group; renderCosts(); }
});
// ширина блока изменилась — график пересчитывается под неё; печать — под ширину листа, с раскрытыми таблицами
new ResizeObserver(() => {
  const box = document.getElementById("costseries");
  if (cost.data && !costPane.hidden && !printing && box.clientWidth && +box.dataset.width !== box.clientWidth) renderSeries();
}).observe(document.getElementById("costseries"));
for (const [event, on] of [["beforeprint", true], ["afterprint", false]])
  addEventListener(event, () => { printing = on; if (cost.data && !costPane.hidden) renderCosts(); });
costRecall();

const READS = { only: "только папка", tree: "с подпапками", none: "снято" };
// право загрузки в личную папку попадает в событие, только когда его выдали или сняли
const homeUpload = e => e.home_upload === undefined ? ""
  : e.home_upload ? "; загрузка в личную папку разрешена" : "; загрузка в личную папку снята";
const ACTIONS = {
  login: e => e.source === "domain" ? "вход под учётной записью домена" : "вход",
  login_failed: e => "неудачная попытка входа" + (e.reason ? `: ${e.reason}` : ""),
  domain_user_new: e => `первый вход через домен — заведён в сервисе: ${e.name}${e.admin ? " (администратор сервиса по группе домена)" : ""}`,
  domain_role: e => e.admin ? "стал администратором сервиса: включён в группу администраторов в домене"
    : "больше не администратор сервиса: исключён из группы администраторов в домене",
  domain_recheck_denied: e => `перепроверка в домене — вход закрыт: ${e.reason}`,
  domain_bind: e => `привязал учётную запись ${e.login} к домену (пароль сервиса стёрт)`,
  domain_check: e => `проверил подключение к домену${e.login ? ` и вход пользователя ${e.login}` : ""}: ${e.ok ? "пройдена" : "есть отказы"}`,
  password: () => "сменил свой пароль",
  request_new: e => `подана заявка на доступ: ${e.name}, логин ${e.login}, почта ${e.email}`,
  request_approve: e => `одобрил заявку на доступ — заведён пользователь ${e.login}${
    e.home_upload ? "; загрузка в личную папку разрешена" : ""}`,
  request_reject: e => `отклонил заявку на доступ (логин ${e.login})`,
  link_sent: e => `выслана ссылка «задать пароль» для ${e.login}: ` +
    ({ invite: "доступ открыт по заявке", reset: "человек нажал «Забыл пароль»", admin: "по кнопке администратора" }[e.why] || e.why),
  link_shown: e => `выпустил ссылку «задать пароль» для ${e.login} и получил её сам, чтобы передать: ` +
    ({ invite: "доступ открыт по заявке", admin: "по кнопке в строке пользователя" }[e.why] || e.why),
  password_link: () => "задал пароль по одноразовой ссылке",
  reset_request: e => `запрошен сброс пароля для «${e.login}»`,
  upload: e => `загрузил запись «${e.file}» (${mb(e.size)})`,
  folder_create: e => `создал папку «${e.folder}»`,
  folder_rename: e => `переименовал папку «${e.folder}» в «${e.to}»`,
  folder_delete: e => `удалил папку «${e.folder}», записей в ней было ${e.records}`,
  access: e => `права на «${e.folder || "все папки"}» для ${e.login}: ` +
    [e.read && `чтение — ${READS[e.read]}`, e.upload && `загрузка — ${READS[e.upload]}`,
     e.manage && `администрирование — ${READS[e.manage]}`].filter(Boolean).join(", "),
  user_create: e => `завёл пользователя ${e.login}${e.admin ? " (администратор сервиса)" : ""}${homeUpload(e)}`,
  user_change: e => `изменил пользователя ${e.login}${e.admin ? " (администратор сервиса)" : ""}${e.password ? ", задал новый пароль" : ""}${homeUpload(e)}`,
  user_delete: e => `удалил пользователя ${e.login}`,
  names: e => `подписал говорящих в записи «${e.file}»`,
  dictionary: e => `изменил словарь ${e.file ? `записи «${e.file}»` : `папки «${e.folder}»`}: участников ${e.people}, сокращений ${e.terms}`,
  series: e => `${e.on ? "включил" : "выключил"} у папки «${e.folder}» серию совещаний`,
  previous_tasks: e => `${e.on ? "вернул" : "убрал"} раздел «Поручения прошлого совещания» в протоколе записи «${e.file}»`,
  key_issue: e => `выпустил ключ доступа «${e.key}»${e.key_kind === "manage" ? " — с управлением сервисом" : ""}`,
  key_revoke: e => `отозвал ключ доступа «${e.key}»${e.owner ? ` пользователя ${e.owner}` : ""}`,
  speech_model: e => e.source === "env"
    ? `вернул модель распознавания к значению из .env: ${e.model} (была ${e.was})`
    : `выбрал модель распознавания для новых записей: ${e.model} (была ${e.was})`,
  record_rename: e => `переименовал запись «${e.file}» в «${e.to.split("/").pop()}»`,
  record_move: e => `перенёс запись «${e.file}» в папку «${e.to.split("/").slice(0, -1).join("/") || "корень"}»`,
  record_delete: e => `удалил запись «${e.file}» в корзину`,
  protocol_sent: e => `отправил протокол записи «${e.file}» на почту: ${(e.to || []).join(", ")} — ` +
    ({ pdf: "вложение PDF", docx: "вложение DOCX", none: "без вложения, ссылкой" }[e.format] || e.format) +
    (e.note ? ", с припиской" : "") + ((e.refused || []).length ? `; не ушло: ${e.refused.join(", ")}` : ""),
  ready_mail: e => `письмо «${e.outcome === "ready" ? "запись готова" : "запись не обработана"}» о записи «${e.file}» — для ${e.to}`,
  mail_failed: e => (e.what === "protocol"
    ? `протокол записи «${e.file}» не отправлен на ${(e.to || []).join(", ")}`
    : e.what === "notice" ? `письмо администраторам о заявке на доступ (логин ${e.login}) не ушло`
    : e.what === "link" ? `письмо со ссылкой «задать пароль» для ${e.login} не ушло`
    : `письмо о записи «${e.file}» для ${e.to} не ушло`) + `: ${e.error}`,
  notify: e => `${e.ready ? "включил" : "отключил"} себе письма о готовности записей`,
  task_done: e => `отметил выполненным поручение «${e.task}»${e.owner ? ` (${e.owner})` : ""} в записи «${e.file}»`,
  task_reopen: e => `снял отметку «выполнено» с поручения «${e.task}»${e.owner ? ` (${e.owner})` : ""} в записи «${e.file}»`,
  brand_logo: e => `заменил логотип организации (${e.format}, ${e.width}×${e.height})`,
  brand_name: e => e.name ? `задал название организации: «${e.name}»` : `убрал название организации (было «${e.was}»)`,
  brand_reset: () => "вернул стандартное оформление",
  template_save: e => ({ page: "изменил на странице", file: "загрузил файлом", default: "вернул стандартный текст",
                         backup: "вернул прошлую версию" }[e.how] || e.how) +
    ` — ${e.file}` + (e.warnings ? `, предупреждений: ${e.warnings}` : ""),
  type_create: e => `добавил свой тип записи «${e.title}» (${e.item})`,
  type_change: e => `изменил описание своего типа «${e.title}» (${e.item})`,
  type_delete: e => `удалил свой тип записи «${e.title}» (${e.item})`,
  task_due: e => `поменял срок поручения «${e.task}»${e.owner ? ` (${e.owner})` : ""} в записи «${e.file}»: ` +
    `${e.was || "без срока"} → ${e.now || "без срока"}`,
};

async function loadActions() {
  let events;
  try { events = (await api("/api/admin/journal?limit=200")).events; }
  catch (err) { return; }
  document.getElementById("actions").innerHTML = !events.length
    ? `<div class="empty">${icon("idle", 17)}действий пока не было</div>`
    : `<div class="scroll"><table><thead><tr><th>когда</th><th>кто</th><th>что</th></tr></thead><tbody>${
        events.map(e => `<tr><td class="num">${e.at.slice(8, 10)}.${e.at.slice(5, 7)}.${e.at.slice(0, 4)} ${e.at.slice(11, 16)}</td>
          <td>${e.by ? esc(e.by) : `<span class="dim">сервис</span>`}${
            e.via === "key" ? ` <span class="dim">· ключом «${esc(e.key || "")}»${e.key_kind === "manage" ? " (с управлением)" : ""}</span>` : ""}</td>
          <td>${esc((ACTIONS[e.kind] || (() => e.kind))(e))}</td></tr>`).join("")}</tbody></table></div>
      <div class="note" style="margin-top:12px">Последние 200 действий. Полный журнал — файл journal.jsonl в папке состояния на сервере.</div>`;
}

restore().then(() => { loadSidebar(); showPane(location.hash.slice(1)); tick(); });
setInterval(tick, 3000);
