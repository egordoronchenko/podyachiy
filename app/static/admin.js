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

/* Отметка «сервис жив» в базе: когда последняя, сколько работает сервис, сколько обработчиков живо. Отметка
   старше трёх периодов или мёртвый обработчик — тревожная плашка, со словами, а не только цветом. */
function beatChip(b) {
  if (!b) return `<span class="chip">отметок «сервис жив» в базе ещё нет</span>`;
  const span = sec => sec >= 86400 ? `${Math.floor(sec / 86400)} д ${Math.floor(sec % 86400 / 3600)} ч`
    : sec >= 3600 ? `${Math.floor(sec / 3600)} ч ${Math.floor(sec % 3600 / 60)} мин` : `${Math.max(0, Math.round(sec / 60))} мин`;
  const late = b.ago_sec > 3 * b.every_sec, dead = b.workers < b.of;
  return `<span class="chip${late || dead ? " alarm" : ""}" title="Сервис раз в ${b.every_sec} с отмечается в базе: так видно, жив ли он">${
    late || dead ? icon("alert", 14) + " " : ""}отметился <b>${b.ago_sec} с назад</b>${late ? " — давно" : ""}, работает <b>${
    span(b.uptime_sec || 0)}</b>, обработчиков <b>${b.workers} из ${b.of}</b>${dead ? " — часть остановилась" : ""}</span>`;
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
    beatChip(state.heartbeat),
  ].join("");
  // предупреждения о хранении: текстом и значком, не только цветом; меняются редко — перерисовываются только при смене
  const stored = state.storage.warnings.map(w => `<div class="nhint warn lines" role="alert">${icon("alert", 14)}<span><b>Внимание:</b> ${esc(w)}.
      Подробно — <a href="/install-guide">в инструкции по установке</a>, раздел «Резервная копия и восстановление».</span></div>`).join("") +
    // тома установки: не подключены с хоста, мало места — текст сервиса уже говорит, что сделать
    state.volumes.warnings.map(w => `<div class="nhint warn lines" role="alert">${icon("alert", 14)}<span><b>Внимание:</b> ${esc(w)}.</span></div>`).join("") +
    // копия из сервиса не удалась: что не так и что сделать — до следующей удачной копии
    backupWarning(state.backups);
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
  paint(document.getElementById("running"), !jobs.length
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
      cancelPart(job) + `</div>`).join(""));

  let q = "";
  if (state.queue.length) {
    q += `<div class="scroll"><table><thead><tr><th>в очереди</th><th>шаблон</th><th>запуск</th><th>действия</th></tr></thead><tbody>` +
      state.queue.map(i => `<tr${qAsk === i.path ? ` class="picked"` : ""}>
        <td class="path">${esc(i.path)}</td>
        <td>${esc(state.templates[i.template] || "по разбору записи")}</td>
        <td class="dim">${i.manual ? "вручную" : "авто"}${i.force ? ", перезапись" : ""}${
          i.model ? `, ${esc(modelTitle(i.model))}` : ""}${
          jobs.some(j => j.file === i.path) ? ", после текущего прогона этой записи" : ""}</td>
        <td><div class="rowacts">${unqueuePart(i.path)}</div></td></tr>`).join("") +
      `</tbody></table></div>`;
  }
  if (state.dropped.length) {
    q += `<div class="scroll" style="margin-top:${state.queue.length ? "12px" : "0"}">
      <table><thead><tr><th>сняты из очереди вручную</th><th>кто и когда</th></tr></thead><tbody>` +
      state.dropped.map(d => `<tr><td class="path">${esc(d.path)}</td><td class="dim">${esc(d.by)}, ${esc(when(d.at))}</td></tr>`).join("") +
      `</tbody></table></div>
      <div class="fhint">Сами в обработку не пойдут: нужна запись — запустите её в «Ручном запуске»; файл изменят — он
        пойдёт в очередь сам, как новый.</div>`;
  }
  if (state.watching.length) {
    q += `<div class="scroll" style="margin-top:${state.queue.length || state.dropped.length ? "12px" : "0"}">
      <table><thead><tr><th>копируется</th><th>размер</th><th>не меняется</th></tr></thead><tbody>` +
      state.watching.map(w => `<tr>
        <td class="path">${esc(w.path)}${w.held ? `<div class="err-text">${icon("alert", 13)} отложен: ${esc(w.held)}</div>` : ""}</td>
        <td class="num">${mb(w.size)}</td>
        <td class="num">${w.quiet_sec} с из ${w.stable_sec}</td></tr>`).join("") +
      `</tbody></table></div>`;
  }
  paint(document.getElementById("queue"), q ||
    `<div class="empty">${icon("clock", 17)}очередь пуста, новых файлов нет</div>`);

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
        if (h.cancelled) tags.push(`<span class="tag warn">прогон отменён: ${esc(h.cancelled.by)}</span>`);
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
  renderBackup();
  renderAdmin();
  syncTemplates();
}

/* Страница перерисовывает сводку каждые три секунды. Блок с кнопками перерисовывается, только когда в нём
   что-то изменилось, а кнопка в фокусе после перерисовки снова получает фокус (по data-focus): иначе фокус
   с клавиатуры терялся бы на каждом опросе. */
function paint(box, html) {
  if (box.dataset.shown === html) return;
  const was = box.contains(document.activeElement) ? document.activeElement.dataset.focus : null;
  box.innerHTML = html;
  box.dataset.shown = html;
  if (was) {
    const again = box.querySelector(`[data-focus="${CSS.escape(was)}"]`);
    if (again) again.focus();
  }
}

/* ---------- очередь: снять запись, отменить прогон ----------
   Оба действия — с подтверждением в самой строке. Снятая новая запись сама в очередь не вернётся, пока файл
   не изменится или её не запустят вручную. Отмена прогона не обрывает идущий запрос поставщику: за него могли
   уже списать деньги, — прогон останавливается перед следующим платным запросом. */
let qAsk = null, qBusy = null;          // запись, которую спросили «снять?»; которую снимают сейчас
let cAsk = null, cBusy = null;          // то же для отмены прогона

function unqueuePart(path) {
  const p = esc(path);
  if (qBusy === path) return `<button type="button" class="danger" disabled>Снимаю…</button>`;
  if (qAsk === path) return `<span>Снять? Сама не вернётся, пока файл не изменят или её не запустят вручную.</span>
    <button type="button" class="ghost" data-qkeep data-focus="qkeep:${p}">Оставить</button>
    <button type="button" class="danger" data-qremove="${p}" data-focus="qremove:${p}">Снять из очереди</button>`;
  return `<button type="button" class="ghost dangerlink" data-qask="${p}" data-focus="qask:${p}"
    aria-label="Снять из очереди ${p}">Снять из очереди</button>`;
}

const CANCEL_HINT = "Прогон остановится перед следующим платным запросом. Идущий запрос поставщику не обрывается: " +
  "за него могли уже списать деньги";

function cancelPart(job) {
  const f = esc(job.file), id = "canceltitle-" + state.running.indexOf(job);
  if (job.cancel) return `<div class="nhint busy lines" style="margin-top:10px">${icon("clock", 14)}<span><b>Отменяется</b>
      (${esc(job.cancel.by)}): прогон остановится перед следующим платным запросом — идущий запрос поставщику
      дорабатывает.</span></div>`;
  if (cAsk === job.file) return `<div class="up dangerbox" style="margin-top:12px" role="alertdialog" aria-labelledby="${id}">
      <b id="${id}">Отменить прогон «${f}»?</b>
      <ul class="plain">
        <li>идущий запрос поставщику не обрывается — за него могли уже списать деньги; прогон остановится перед
          следующим платным запросом, это может занять несколько минут;</li>
        <li>оплаченное до отмены остаётся в расходах и на диске: запуск заново возьмёт готовое без новой оплаты;</li>
        <li>по расписанию запись не повторится и писем о ней не будет — запустить её снова можно в «Ручном запуске»;</li>
        <li>готовая прежде запись останется готовой, с прежним протоколом.</li>
      </ul>
      <div class="row"><button type="button" class="ghost" data-ckeep data-focus="ckeep:${f}">Не отменять</button>
        <button type="button" class="danger" data-cancel="${f}" data-focus="cancel:${f}"${cBusy === job.file ? " disabled" : ""}>${
          cBusy === job.file ? "Отменяю…" : "Отменить прогон"}</button></div>
    </div>`;
  return `<div class="row jobacts"><button type="button" class="ghost dangerlink" data-cask="${f}" data-focus="cask:${f}"
      title="${CANCEL_HINT}" aria-label="Отменить прогон ${f}">Отменить прогон</button></div>`;
}

async function queueAct(url, file, done, msgId) {
  try {
    await api(url, { file });
    say(msgId, "ok", done);
  } catch (err) { say(msgId, "err", err.message); }
  await tick();
}

document.getElementById("queue").addEventListener("click", async e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.qask) {
    qAsk = b.dataset.qask; say("qmsg", "", "");
    render();
    document.querySelector("#queue [data-qkeep]").focus();          // случайный Enter ничего не снимет
  } else if ("qkeep" in b.dataset) {
    const path = qAsk;
    qAsk = null;
    render();
    const back = document.querySelector(`#queue [data-qask="${CSS.escape(path)}"]`);
    if (back) back.focus();
  } else if (b.dataset.qremove) {
    const path = b.dataset.qremove;
    qBusy = path; qAsk = null;
    render();
    await queueAct("/api/admin/queue/remove", path, `Запись ${path} снята из очереди.`, "qmsg");
    qBusy = null;
    render();
  }
});

document.getElementById("running").addEventListener("click", async e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.cask) {
    cAsk = b.dataset.cask; say("rmsg", "", "");
    render();
    document.querySelector("#running [data-ckeep]").focus();
  } else if ("ckeep" in b.dataset) {
    const file = cAsk;
    cAsk = null;
    render();
    const back = document.querySelector(`#running [data-cask="${CSS.escape(file)}"]`);
    if (back) back.focus();
  } else if (b.dataset.cancel) {
    const file = b.dataset.cancel;
    cBusy = file;
    render();
    await queueAct("/api/admin/queue/cancel", file,
      `Отмена прогона ${file} запрошена: он остановится перед следующим платным запросом.`, "rmsg");
    cBusy = null; cAsk = null;
    render();
  }
});

/* ---------- резервная копия ----------
   Копию делает сам сервис: по расписанию и кнопкой. Форма перерисовывается, только когда расписание на сервере
   изменилось и в форме нет несохранённой правки. Сумм здесь нет; что не удалось — плашкой вверху раздела. */
const BACKUP_DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"];
const HOW = { command: "командой", schedule: "по расписанию", page: "по кнопке" };
let bkSeen = "", bkDirty = false, bkBusy = "";      // bkBusy: "save" | "now" — идёт запрос

function backupWarning(b) {
  const f = b.failed;
  if (!f) return "";
  return `<div class="nhint warn lines" role="alert">${icon("alert", 14)}<span><b>Резервная копия не сделана</b>
    ${esc(when(f.at))} (${esc(HOW[f.how] || f.how)}${f.by ? `, ${esc(f.by)}` : ""}): ${esc(f.why)}.
    ${b.plan.on ? `Следующая попытка — по расписанию${b.next ? `, ${esc(when(b.next))}` : ""}; можно и сейчас — ` : "Повторить — "}
    кнопка «Сделать копию сейчас» в разделе «Резервная копия» ниже.</span></div>`;
}

function backupState() {
  const b = state.backups, last = state.storage.backup;
  const parts = [last ? `Последняя копия — <b>${esc(when(last.at))}</b> (${esc(HOW[last.how] || "командой")}${
      last.inbox ? "" : ", без исходных записей"})` : "Копий ещё не было"];
  if (b.running) parts.push(`<b>Копия делается</b> с ${esc(hhmm(b.running.started))} (${esc(HOW[b.running.how] || b.running.how)}${
    b.running.by ? `, ${esc(b.running.by)}` : ""}) — обработка записей при этом идёт`);
  else parts.push(b.plan.on && b.next ? `следующая по расписанию — <b>${esc(when(b.next))}</b>` : "по расписанию копия не делается");
  return parts.join("; ") + ".";
}

function renderBackup() {
  const b = state.backups;
  const line = backupState(), box = document.getElementById("backupstate");
  if (box.dataset.shown !== line) { box.innerHTML = line; box.dataset.shown = line; }
  const now = document.getElementById("bknow");
  if (now) {
    now.disabled = !!b.running || bkBusy === "now";
    now.textContent = b.running || bkBusy === "now" ? "Копия делается…" : "Сделать копию сейчас";
  }
  const seen = JSON.stringify(b.plan);
  if (seen === bkSeen || bkDirty) return;
  bkSeen = seen;
  const p = b.plan;
  document.getElementById("backup").innerHTML = `
    <label class="check" for="bkon"><input type="checkbox" id="bkon"${p.on ? " checked" : ""}> делать копию по расписанию</label>
    <div class="bkgrid">
      <div><label for="bktime">Время</label>
        <input type="time" id="bktime" value="${esc(p.time)}" step="60" required aria-describedby="bktime-hint"></div>
      <div><label for="bkevery">Как часто</label>
        <select id="bkevery"><option value="day"${p.every === "day" ? " selected" : ""}>каждый день</option>
          <option value="week"${p.every === "week" ? " selected" : ""}>раз в неделю</option></select></div>
      <div><label for="bkday">День недели</label>
        <select id="bkday"${p.every === "week" ? "" : " disabled"}>${BACKUP_DAYS.map((d, n) =>
          `<option value="${n}"${n === p.weekday ? " selected" : ""}>${d}</option>`).join("")}</select></div>
      <div><label for="bkkeep">Держать копий</label>
        <input type="number" id="bkkeep" min="1" max="365" step="1" inputmode="numeric" value="${p.keep}" aria-describedby="bkkeep-hint"></div>
    </div>
    <div class="fhint" id="bktime-hint">Время — по часовому поясу сервиса (TZ в .env). Сервис был выключен в это время —
      одна копия, когда он снова заработает; пропущенные копии он не догоняет.</div>
    <div class="fhint" id="bkkeep-hint">Копии сверх этого числа, самые старые, стираются после каждой новой.</div>
    <label class="check" for="bkinbox"><input type="checkbox" id="bkinbox" aria-describedby="bkinbox-hint"${p.inbox ? " checked" : ""}>
      с исходными записями (папка inbox)</label>
    <div class="fhint" id="bkinbox-hint">Без них архив намного меньше, но записи из него заново с распознаванием не обработать.</div>
    <div class="ferr" id="bkerr" role="alert"></div>
    <div class="row"><button type="button" id="bksave">Сохранить расписание</button>
      <button type="button" class="ghost" id="bknow" aria-describedby="bknow-hint"${b.running ? " disabled" : ""}>${
        b.running ? "Копия делается…" : "Сделать копию сейчас"}</button></div>
    <div class="fhint" id="bknow-hint">Копия в фоне, обработка записей не останавливается. С исходными записями или без и
      сколько держать — как в сохранённом расписании; его не сохраняли — с записями, старые копии не стираются.</div>
    <div class="msg" id="bkmsg" role="status"></div>
    <div class="note">Архив ложится в папку копий на сервере (BACKUP_DIR). Файлов настроек (.env, compose.yml) в нём нет —
      их копируют отдельно; как проверить архив и восстановиться — <a href="/install-guide">инструкция по установке</a>.</div>
    <p class="fhint">${p.by ? `Расписание сохранил ${esc(p.by)}, ${esc(when(p.at))}.` : "Расписание ещё не сохраняли."}</p>`;
}

const backupBox = document.getElementById("backup");
backupBox.addEventListener("input", () => { bkDirty = true; });
backupBox.addEventListener("change", e => {
  bkDirty = true;
  if (e.target.id === "bkevery") document.getElementById("bkday").disabled = e.target.value !== "week";
});
backupBox.addEventListener("click", async e => {
  if (e.target.id === "bksave" && !bkBusy) {
    const btn = e.target, plan = {
      on: document.getElementById("bkon").checked, time: document.getElementById("bktime").value,
      every: document.getElementById("bkevery").value, weekday: Number(document.getElementById("bkday").value),
      inbox: document.getElementById("bkinbox").checked, keep: Number(document.getElementById("bkkeep").value) };
    bkBusy = "save"; btn.disabled = true; btn.textContent = "Сохраняю…";
    document.getElementById("bkerr").textContent = "";
    try {
      state.backups = (await api("/api/admin/backup/plan", plan)).backups;
      bkDirty = false; bkSeen = "";
      renderBackup();
      say("bkmsg", "ok", plan.on ? "Расписание сохранено." : "Сохранено: по расписанию копия не делается.");
    } catch (err) {
      document.getElementById("bkerr").innerHTML = `${icon("alert", 14)}<span>Не сохранено: ${esc(err.message)}</span>`;
      btn.disabled = false; btn.textContent = "Сохранить расписание";
    }
    bkBusy = "";
  } else if (e.target.id === "bknow" && !bkBusy) {
    bkBusy = "now";
    renderBackup();
    try {
      state.backups = (await api("/api/admin/backup/now", {})).backups;
      say("bkmsg", "ok", "Копия начата — итог появится здесь и в журнале работы.");
    } catch (err) { say("bkmsg", "err", "Копия не начата: " + err.message); }
    bkBusy = "";
    renderBackup();
  }
});

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
