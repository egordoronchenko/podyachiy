let data = null, navDirs = [], folder = "", filter = "";
let query = "", results = null, searching = false, qtimer = null;

initSidebar("records");
initTheme();
initViewer();
document.querySelector(".blankmark").innerHTML = `<img src="${LOGO}" alt="" width="100">`;

// Запись читается в правой колонке; на узком экране она открывается поверх списка.
function syncReading() {
  document.body.classList.toggle("reading", !!viewFile);
  if (data) renderFiles();
}

// Папка и открытый протокол живут в адресе: ссылку можно отправить коллеге,
// а «назад» в браузере закрывает просмотрщик и возвращает в прошлую папку.
function urlFor(path, file, kind) {
  const p = new URLSearchParams();
  if (query) p.set("q", query);
  if (path) p.set("path", path);
  if (file) {
    p.set("open", file);
    if (kind && kind !== "summary") p.set("kind", kind);
  }
  const q = p.toString();
  return q ? "/?" + q : "/";
}

/* Держим адрес в соответствии с тем, что показано. Открытие протокола — отдельный
   шаг истории, чтобы «назад» его закрывало; смена вкладки внутри просмотрщика
   лишним шагом не становится. Закрытие — это и есть шаг назад, но только если
   шаг делали мы: по прямой ссылке «назад» должен уводить со страницы, как обычно. */
onViewerChange = function (what) {
  const url = urlFor(folder, viewFile, viewKind);
  if (what === "open") history.pushState({ path: folder, pushed: true }, "", url);
  else if (what === "close" && history.state && history.state.pushed) history.back();
  else history.replaceState(history.state, "", url);
  syncReading();
};

function goTo(path, push) {
  folder = path;
  if (push) history.pushState({ path }, "", urlFor(path));
  load();
}

// Адрес — источник правды: после «назад» и «вперёд» подгоняем под него и папку,
// и просмотрщик, ничего в историю не дописывая.
addEventListener("popstate", () => {
  const q = new URLSearchParams(location.search);
  const path = q.get("path") || "";
  const open = q.get("open") || "";
  const kind = q.get("kind") || "summary";
  const text = q.get("q") || "";
  if (text !== query) {
    query = text;
    document.getElementById("q").value = text;
    runSearch();
  }
  syncReading();
  if (path !== folder) { folder = path; load(); }
  if (open && (open !== viewFile || kind !== viewKind)) openViewer(open, kind, true);
  else if (!open && viewFile) closeViewer(true);
});


/* Список: «все записи» — всё открытое человеку из всех папок сразу; папка — только её
   содержимое. Папки верхнего уровня для меню берём отдельно, раз в обновление. */
async function load() {
  try {
    const here = folder ? `path=${encodeURIComponent(folder)}` : "flat=true";
    const [list, root] = await Promise.all([api(`/api/browse?${here}`), folder ? api("/api/browse") : null]);
    data = list;
    navDirs = (root || list).dirs;
    render();
  } catch (e) {
    const pill = document.getElementById("livepill");
    pill.hidden = false;
    pill.className = "pill down";
    pill.innerHTML = `<span class="dot"></span>сервис недоступен`;
  }
}

// Куда попадёт загруженная запись: в открытую папку, а из общего списка — в личную.
// null — сюда человеку загружать нельзя: чтение папки права загрузки не даёт (data.upload.denied — почему).
function uploadDir() {
  if (!data) return null;
  if (data.upload.enabled) return folder;
  return !folder && data.home_upload && me && me.home ? me.home : null;
}

function render() {
  showService(data.service);
  renderNav(navDirs, folder);

  // хлебные крошки
  const parts = data.path ? data.path.split("/") : [];
  let acc = "";
  document.getElementById("crumbs").innerHTML = !parts.length ? "" :
    `<a href="/" data-go="">${icon("home", 14)}Все записи</a>` +
    parts.map(p => {
      acc = acc ? `${acc}/${p}` : p;
      return `<span class="sep">/</span><a href="/?path=${encodeURIComponent(acc)}" data-go="${esc(acc)}">${esc(p)}</a>`;
    }).join("");

  document.getElementById("folderTitle").textContent = data.path ? data.path.split("/").pop() : "Все записи";
  document.getElementById("mkbtn").hidden = !data.mkdir;
  const tasksBtn = document.getElementById("tasksbtn");
  tasksBtn.hidden = !data.path;
  tasksBtn.href = "/tasks?dir=" + encodeURIComponent(data.path);
  document.getElementById("dcbtn").hidden = !data.path;
  document.getElementById("acbtn").hidden = !data.manage;
  // панель доступа относится к одной папке: ушли из неё — панель закрывается
  if (accessFor !== null && accessFor !== data.path) closeAccess();
  seriesButton();
  if (seriesFor !== null && seriesFor !== data.path) closeSeries(false);

  const upbtn = document.getElementById("upbtn");
  const target = uploadDir();
  // тому, кому загружать некуда вовсе, кнопка не показывается; есть куда, но не сюда — она
  // неактивна, и причина написана под ней: подсказку при наведении с телефона не увидеть
  upbtn.hidden = !data.upload.anywhere;
  document.getElementById("uphint").hidden = !data.upload.anywhere;
  upbtn.disabled = target === null;
  upbtn.title = target === null ? "" : `Загрузить запись ${target ? "в папку «" + target + "»" : "в корень"}. Файл можно и перетащить в список`;
  const upwhy = document.getElementById("upwhy");
  upwhy.hidden = upbtn.hidden || target !== null;
  upwhy.textContent = upFirst(data.upload.denied);
  if (target !== null && dropNote) { dropNote = ""; renderUploads(); }
  if (!upbtn.innerHTML) {
    upbtn.innerHTML = icon("upload", 16) + "Загрузить запись";
    document.getElementById("upfile").accept = data.upload.exts.join(",");
  }

  // подпапки открытой папки; в общем списке папки уже есть в меню
  document.getElementById("dirs").innerHTML = !data.path || !data.dirs.length ? "" :
    data.dirs.map(d => `<a class="it dir" href="/?path=${encodeURIComponent(d.path)}" data-go="${esc(d.path)}">
        ${icon("folder", 17)}<span class="t">${esc(d.name)}</span>
        <span class="m">${d.files} ${plural(d.files, "запись", "записи", "записей")}${d.busy ? ` · в работе ${d.busy}` : ""}</span>
      </a>`).join("");
  renderFiles();
}

const SOURCE_LABELS = { changed: "исходник изменился", missing: "исходник отсутствует" };
const SOURCE_HINTS = {
  changed: "Файл записи во входной папке изменился после обработки: его докопировали или перезаписали. Протокол и " +
    "расшифровка сделаны по прежнему файлу, заново сервис сам его не распознаёт — это стоило бы денег. Нужно по " +
    "новому файлу — администратор сервиса запускает запись вручную с перезаписью.",
  missing: "Файла записи нет во входной папке: его убрали или переименовали мимо сервиса. Протокол и расшифровка на " +
    "месте, но пересобрать их не выйдет, пока файл не вернут.",
};

function renderFiles() {
  const busyHere = (data.pending || []).filter(p => !data.path || p.dir === data.path);
  const box = document.getElementById("files");
  if (!data.files.length && !busyHere.length) {
    box.innerHTML = data.path && data.dirs.length ? "" :
      `<div class="empty" style="padding:18px 16px">${icon("doc", 17)}${data.path ? "в этой папке пока нет записей" : data.upload.anywhere ? "записей пока нет — загрузите первую" : "записей пока нет"}</div>`;
    return;
  }
  box.innerHTML = busyHere.map(p => {
      const label = { copying: "копируется", held: "отложена", queued: "в очереди", running: "обрабатывается" }[p.state];
      // отложенная (похожа на недокопированную, ждёт решения администратора) — почему, словами от сервиса
      const note = p.state === "held" ? p.note
        : p.state === "copying"
        ? `не меняется ${p.quiet_sec} с из ${p.stable_sec}`
        : p.state === "running"
          ? (p.chunks_total ? `часть ${p.chunks_done} из ${p.chunks_total}` : (p.step || "готовлю звук"))
          : (p.manual ? "запущено вручную" : "ждёт очереди");
      return `<div class="it">
        <div class="t">${esc(p.name)}</div>
        <div class="g">${esc(note)}</div>
        ${p.state === "running" && p.chunks_total
          ? `<div class="bar" style="margin-top:4px"><i style="width:${100 * p.chunks_done / p.chunks_total}%"></i></div>` : ""}
        <div class="m"><span class="badge ${p.state === "held" ? "no_audio" : "working"}">${
          icon(p.state === "copying" ? "copy" : p.state === "held" ? "alert" : "clock", 13)}${label}</span></div>
      </div>`;
    }).join("") +
    data.files.map(f => {
      const tags = [];
      if (f.status !== "ok") tags.push(`<span class="badge ${esc(f.status)}">${icon(STATUS_ICONS[f.status] || "alert", 13)}${
        esc(STATUS_LABELS[f.status] || f.status)}</span>`);
      // файл записи во входной папке: изменился после обработки или пропал — словами на метке, подробно — в подсказке
      if (f.source) tags.push(`<span class="tag warn" title="${esc(SOURCE_HINTS[f.source] || "")}">${
        esc(SOURCE_LABELS[f.source] || f.source)}</span>`);
      if (f.speakers_unreliable) tags.push(`<span class="tag warn">метки ненадёжны</span>`);
      if (f.crowded_meeting) tags.push(`<span class="tag warn">многолюдно</span>`);
      if (f.type_unsure) tags.push(`<span class="tag warn" title="${
        esc((f.type_reason || "") + (f.type_confidence ? ` (уверенность ${f.type_confidence})` : ""))
        }">шаблон выбран неуверенно</span>`);
      if (f.timestamps_unknown) tags.push(`<span class="tag warn">${
        f.timestamps_unknown} ${plural(f.timestamps_unknown, "метка времени", "метки времени", "меток времени")} не из расшифровки</span>`);
      const openable = f.kinds.length > 0;
      const meta = [f.finished_at ? f.finished_at.slice(0, 10) : "", f.audio_duration_sec ? dur(f.audio_duration_sec) : "",
                    f.speakers ? `${f.speakers} ${plural(f.speakers, "голос", "голоса", "голосов")}` : "",
                    f.speech_model ? `распознано ${f.speech_model.split("/").pop()}` : "",
                    !data.path && f.dir ? f.dir : ""].filter(Boolean).map(esc).join(" · ");
      const inner = `<div class="t">${esc(f.title || f.name)}</div>
        ${f.title ? `<div class="n">${esc(f.name)}</div>` : ""}
        ${f.gist ? `<div class="g">${esc(f.gist)}</div>` : ""}
        ${f.error ? `<div class="err-text">${esc(f.error)}</div>` : ""}
        <div class="m">${meta}</div>
        ${tags.length ? `<div class="tags">${tags.join("")}</div>` : ""}`;
      return openable
        ? `<a class="it open${f.file === viewFile ? " on" : ""}" data-file="${esc(f.file)}" role="button" tabindex="0"
              href="${esc(urlFor(folder, f.file))}">${inner}</a>`
        : `<div class="it">${inner}</div>`;
    }).join("");
}

/* Поиск идёт по всем записям сразу, поэтому на время поиска дерево папок
   уходит: показывать одновременно «здесь в папке» и «везде» — путать. */
async function runSearch() {
  if (query.length < 2) { results = null; searching = false; renderSearch(); return; }
  const asked = query;
  searching = true;
  renderSearch();
  try {
    const found = await api(`/api/search?q=${encodeURIComponent(query)}`);
    if (asked !== query) return;          // пока искали, запрос успел смениться
    results = found;
  } catch (e) {
    if (asked !== query) return;
    results = { error: e.message || String(e) };
  }
  searching = false;
  renderSearch();
}


function renderSearch() {
  const box = document.getElementById("results");
  document.getElementById("browse").hidden = query.length >= 2;
  if (query.length < 2) { box.innerHTML = ""; return; }
  const say = (ic, html) => { box.innerHTML = `<div class="empty" style="padding:18px 16px">${icon(ic, 17)}${html}</div>`; };
  if (searching) return say("search", `ищу «${esc(query)}» во всех записях…`);
  if (!results) { box.innerHTML = ""; return; }
  if (results.error) return say("alert", esc(results.error));
  if (!results.files.length) return say("search", `по запросу «${esc(query)}» ничего не нашлось
      среди ${results.scanned} ${plural(results.scanned, "записи", "записей", "записей")}`);
  const where = k => ({ summary: "в протоколе", triage: "в разборе" })[k] || "в расшифровке";
  box.innerHTML = `<div class="dim" style="font-size:12.5px;padding:10px 16px 4px">
      нашлось в ${results.files.length} ${plural(results.files.length, "записи", "записях", "записях")}
      из ${results.scanned}${results.more ? `, показаны первые ${results.files.length}` : ""}</div>` +
    results.files.map(f => `
      <a class="it open${f.file === viewFile ? " on" : ""}" href="${esc(urlFor(f.dir, f.file, f.open_kind))}"
         data-file="${esc(f.file)}" data-kind="${esc(f.open_kind)}" role="button" tabindex="0">
        <div class="t">${esc(f.name)}</div>
        <div class="m">${f.dir ? esc(f.dir) + " · " : ""}${f.finished_at ? f.finished_at.slice(0, 10) + " · " : ""}${
          Object.keys(f.kinds).map(where).join(" и ")}</div>
        ${f.snippets.map(s => `<div class="snip">${esc(s.before)}<mark>${esc(s.match)}</mark>${esc(s.after)}</div>`).join("")}
      </a>`).join("");
}

/* ---------- загрузка записей ----------
   Файл уходит частями, по одной записи за раз. Сервер помнит, сколько принял,
   поэтому после обрыва связи или закрытой вкладки тот же файл продолжает
   грузиться с принятого места, а не с начала. */
const ups = [];
let upBusy = false, upSeq = 0;
const big = n => n >= 2 ** 30 ? (n / 2 ** 30).toFixed(2).replace(".", ",") + " ГБ" : mb(n);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const upUrl = u => `/api/upload?dir=${encodeURIComponent(u.dir)}&name=${
  encodeURIComponent(u.file.name)}&size=${u.file.size}&stamp=${u.file.lastModified}`;

function addUploads(list) {
  const rules = data && data.upload;
  const target = uploadDir();
  if (!rules || target === null) return;
  for (const file of list) {
    // словарь записи при загрузке — тому, кто в этой папке хозяин (личная папка — своя всегда)
    const u = { id: ++upSeq, file, dir: target, sent: 0, state: "wait", note: "", marks: [],
                owns: target === folder ? data.owns : true, dict: null, dictNote: "" };
    const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
    if (!rules.exts.includes(ext)) fail(u, "это не запись: нужен звуковой или видеофайл");
    else if (!file.size) fail(u, "файл пустой");
    else if (file.size > rules.max_bytes) fail(u, `файл больше предела ${big(rules.max_bytes)}`);
    ups.push(u);
  }
  renderUploads();
  pump();
}

function fail(u, why) { u.state = "failed"; u.note = why; }

async function pump() {
  if (upBusy) return;
  const u = ups.find(x => x.state === "wait");
  if (!u) return;
  upBusy = true;
  await send(u);
  upBusy = false;
  renderUploads();
  pump();
}

async function send(u) {
  u.state = "run";
  u.abort = new AbortController();
  let chunk = 0, misses = 0;
  while (u.state === "run") {
    try {
      if (!chunk) {                       // начало или продолжение: спрашиваем, сколько уже принято
        const have = await api(upUrl(u));
        u.sent = have.offset; chunk = have.chunk; u.marks = [];
      }
      const r = await fetch(`${upUrl(u)}&offset=${u.sent}`, {
        method: "PUT", body: u.file.slice(u.sent, u.sent + chunk), signal: u.abort.signal });
      if (r.status === 409 && ++misses < 4) { chunk = 0; continue; }   // часть не встык — сверяемся
      if (!r.ok) {
        let detail = "HTTP " + r.status;
        try { detail = (await r.json()).detail || detail; } catch (e) {}
        throw new Error(detail);
      }
      const got = await r.json();
      u.sent = got.offset; u.note = ""; misses = 0;
      u.marks.push([performance.now(), u.sent]);
      while (u.marks.length > 2 && u.marks[u.marks.length - 1][0] - u.marks[0][0] > 15000) u.marks.shift();
      if (got.done) { u.state = "done"; u.rel = got.file; if (u.dict) await sendDict(u); load(); }
    } catch (e) {
      if (u.state !== "run") break;                           // отменили
      if (!(e instanceof TypeError)) { fail(u, e.message || String(e)); break; }
      // TypeError у fetch — это сеть: ждём и продолжаем с того, что сервер успел принять
      if (++misses > 8) { u.state = "paused"; u.note = "связь с сервисом пропала"; break; }
      u.note = "нет связи, пробую снова…"; chunk = 0;
      renderUploads();
      await sleep(Math.min(2000 * misses, 15000));
    }
    renderUploads();
  }
}

// Словарь, вписанный, пока запись загружалась, уходит на сервер сразу после неё — до обработки
async function sendDict(u) {
  try {
    const d = await api("/api/dictionary", { file: u.rel, ...u.dict });
    u.dictNote = `словарь записи сохранён: ${dictSize(d.people.length, d.terms.length) || "пуст"}`;
    u.dict = null;
  } catch (e) { u.dictNote = "словарь записи не сохранён: " + e.message; }
}

function uploadDict(u) {
  if (u.state === "done") return openDict({ file: u.rel });
  openDict({ file: (u.dir ? u.dir + "/" : "") + u.file.name, draft: u.dict || { people: "", terms: "" },
             onDraft: async typed => {
               u.dict = typed.people.trim() || typed.terms.trim() ? typed : null;
               // пока человек вписывал, запись могла догрузиться — тогда ждать больше нечего
               if (u.dict && u.state === "done") await sendDict(u);
               renderUploads();
             } });
}

const upFirst = text => text ? text[0].toUpperCase() + text.slice(1) : "";
let dropNote = "";              // отказ на перетаскивание туда, куда человеку загружать нельзя

function renderUploads() {
  document.getElementById("uploads").innerHTML = (dropNote ? `<div class="up failed" role="alert">
      <div class="uphead"><span class="upname">Запись не загружена</span>
        <button class="ghost" data-do="undrop">Убрать</button></div>
      <div class="err-text upnote">${esc(dropNote)}</div></div>` : "") + ups.map(u => {
    const pct = u.file.size ? 100 * u.sent / u.file.size : 0;
    let note = u.note, action = "Убрать";
    if (u.state === "wait") { note = "ждёт своей очереди"; action = "Отменить"; }
    if (u.state === "run") {
      action = "Отменить";
      const m = u.marks, span = m.length > 1 ? (m[m.length - 1][0] - m[0][0]) / 1000 : 0;
      const speed = span > 0 ? (m[m.length - 1][1] - m[0][1]) / span : 0;
      const left = speed > 0 ? (u.file.size - u.sent) / speed : null;
      note = note || `${big(u.sent)} из ${big(u.file.size)}` +
        (speed ? ` · ${big(speed)}/с · осталось ${left < 90 ? Math.ceil(left) + " с" : Math.ceil(left / 60) + " мин"}` : "");
    }
    if (u.state === "done") note = "загружена — сервис возьмёт её в работу в течение минуты";
    if (u.dictNote) note += ` · ${u.dictNote}`;
    else if (u.dict) note += " · словарь записи вписан — сохранится вместе с ней";
    if (u.state === "paused") note += ` — принято ${big(u.sent)} из ${big(u.file.size)}`;
    return `<div class="up ${u.state}">
      <div class="uphead">
        <span class="upname">${esc(u.file.name)}</span>
        <span class="dim">${u.dir ? "в папку " + esc(u.dir) : "в корень"}</span>
        ${u.owns && u.state !== "failed" ? `<button class="ghost" data-up="${u.id}" data-do="dict" aria-haspopup="dialog"
          title="Кто был на этом совещании и какие в нём сокращения — необязательно">Словарь записи</button>` : ""}
        ${u.state === "paused" ? `<button class="ghost" data-up="${u.id}" data-do="resume">Продолжить</button>` : ""}
        <button class="ghost" data-up="${u.id}" data-do="drop">${action}</button>
      </div>
      ${u.state === "failed" ? "" : `<div class="bar"><i style="width:${pct}%"></i></div>`}
      <div class="${u.state === "failed" ? "err-text" : "dim"} upnote">${esc(note)}</div>
    </div>`;
  }).join("");
}

document.getElementById("uploads").addEventListener("click", e => {
  if (e.target.closest('[data-do="undrop"]')) { dropNote = ""; renderUploads(); return; }
  const b = e.target.closest("[data-up]");
  if (!b) return;
  const u = ups.find(x => x.id === +b.dataset.up);
  if (!u) return;
  if (b.dataset.do === "dict") { uploadDict(u); return; }
  if (b.dataset.do === "resume") { u.state = "wait"; u.note = ""; renderUploads(); pump(); return; }
  const unfinished = ["wait", "run", "paused"].includes(u.state) && u.sent < u.file.size;
  u.state = "gone";
  if (u.abort) u.abort.abort();
  ups.splice(ups.indexOf(u), 1);
  // отменённую загрузку сервер хранить не должен; сбой тут не страшен: недогрузка сама уйдёт через неделю
  if (unfinished) fetch(upUrl(u), { method: "DELETE" }).catch(() => {});
  renderUploads();
});

/* ---------- доступ к папке ----------
   Открывает и закрывает папку людям администратор сервиса и локальный администратор
   этой папки. Галочка действует на саму папку со всеми подпапками. */
let accessFor = null;

function closeAccess() {
  accessFor = null;
  document.getElementById("access").hidden = true;
}

function drawAccess(d) {
  const box = document.getElementById("access");
  box.hidden = false;
  box.innerHTML = `<div class="veil" id="acveil"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="actitle">
    <header><h2 id="actitle">Доступ к папке «${esc(accessFor)}»</h2>
      <button class="ghost icon" id="acclose" aria-label="Закрыть">${icon("close", 15)}</button></header>
    <div class="body">${accessTable(d.users, accessFor, d.can_manage)}
      <div class="msg" id="acmsg" role="status"></div></div>
  </div></div>`;
}

document.getElementById("dcbtn").onclick = () => openDict({ folder: data.path });
document.getElementById("acbtn").onclick = async () => {
  if (accessFor !== null) return closeAccess();
  accessFor = data.path;
  try { drawAccess(await api(`/api/access?dir=${encodeURIComponent(accessFor)}`)); }
  catch (e) { closeAccess(); alert(e.message); }
};
document.getElementById("access").addEventListener("click", e => {
  if (e.target.closest("#acclose") || e.target.id === "acveil") closeAccess();
});
// Escape закрывает окно доступа, а не запись под ним: перехватываем раньше просмотрщика
addEventListener("keydown", e => {
  if (e.key === "Escape" && accessFor !== null) { e.stopPropagation(); closeAccess(); }
}, true);
document.getElementById("access").addEventListener("change", async e => {
  const select = e.target.closest("select[data-kind]");
  if (!select) return;
  try {
    drawAccess(await saveScope(select, accessFor));
    say("acmsg", "ok", "Сохранено");
  } catch (err) { say("acmsg", "err", err.message); }
});

/* ---------- серия совещаний ----------
   Признак папки: в конец протокола новой записи сервис дописывает незакрытые поручения
   предыдущей записи этой папки. Он включён у каждой папки, пока его не выключили; у корня его нет,
   и кнопки там нет. Видят его все, кому папка открыта, — на кнопке «Серия» и в её окне; переключает
   хозяин папки, сразу, без кнопки «Сохранить». */
let seriesFor = null;

function seriesButton() {
  const btn = document.getElementById("srbtn"), state = document.getElementById("srstate");
  btn.hidden = !data.path;
  state.textContent = data.series ? "вкл" : "выкл";
  state.classList.toggle("on", !!data.series);
  btn.setAttribute("aria-label", `Серия совещаний: ${data.series ? "включена" : "выключена"}`);
}

// back — вернуть фокус на кнопку «Серия»: окно закрыл человек, а не уход в другую папку
function closeSeries(back = true) {
  if (seriesFor === null) return;
  seriesFor = null;
  const box = document.getElementById("series");
  box.hidden = true;
  box.innerHTML = "";
  if (back) document.getElementById("srbtn").focus();
}

function drawSeries(note) {
  const box = document.getElementById("series"), on = !!data.series, edit = !!data.owns;
  box.hidden = false;
  box.innerHTML = `<div class="veil" id="srveil"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="srtitle">
    <header><h2 id="srtitle">Папка «${esc(seriesFor.split("/").pop())}»</h2>
      <button class="ghost icon" id="srclose" aria-label="Закрыть">${icon("close", 15)}</button></header>
    <div class="body">
      <label class="switchrow" for="srswitch">
        <input type="checkbox" role="switch" id="srswitch"${on ? " checked" : ""}${edit ? "" : " disabled"}
          aria-describedby="srhint">
        <span><b>Серия совещаний</b><span class="dim"> — ${on ? "включена" : "выключена"}</span></span>
      </label>
      <p class="fhint" id="srhint">В конец протокола новой записи добавляются незакрытые поручения предыдущей записи этой папки.</p>
      <p class="note">Выключите, если в папке лежат не связанные между собой совещания. Подпапки — отдельные серии
        со своим переключателем. Готовые протоколы от переключателя не меняются: раздел у записи убирают кнопкой
        «Убрать этот раздел» на вкладке «Протокол».</p>
      ${edit ? "" : `<p class="dim signby">Менять может владелец папки или её администратор.</p>`}
      <div class="msg${note ? " " + note.kind : ""}" id="srmsg" role="status">${note ? esc(note.text) : ""}</div>
      <div class="row"><button type="button" class="ghost" id="srdone">Закрыть</button></div>
    </div></div></div>`;
  (edit ? document.getElementById("srswitch") : document.getElementById("srdone")).focus();
}

document.getElementById("srbtn").onclick = () => {
  if (seriesFor !== null) return closeSeries();
  seriesFor = data.path;
  drawSeries();
};
const seriesBox = document.getElementById("series");
let seriesDownOnVeil = false;
seriesBox.addEventListener("mousedown", e => { seriesDownOnVeil = e.target.id === "srveil"; });
seriesBox.addEventListener("click", e => {
  if (e.target.closest("#srclose, #srdone") || (e.target.id === "srveil" && seriesDownOnVeil)) closeSeries();
});
seriesBox.addEventListener("change", async e => {
  const sw = e.target.closest("#srswitch");
  if (!sw) return;
  const on = sw.checked;
  sw.disabled = true;
  say("srmsg", "", "Сохраняю…");
  try {
    const d = await api("/api/folder/series", { dir: seriesFor, on });
    if (seriesFor === null || d.path !== data.path) return;      // пока сохраняли, окно закрыли или ушли в другую папку
    data.series = d.series;
    seriesButton();
    drawSeries({ kind: "ok", text: d.series
      ? "Сохранено: папка — серия совещаний. Раздел появится в протоколах, собранных с этой минуты."
      : "Сохранено: папка — не серия. Новые протоколы её записей придут без раздела «Поручения прошлого совещания»." });
  } catch (err) {
    sw.checked = !on;
    sw.disabled = false;
    say("srmsg", "err", err.message);
    sw.focus();
  }
});
// Escape закрывает окно серии, а не запись под ним; Tab ходит по кругу внутри окна
addEventListener("keydown", e => {
  if (seriesFor === null || seriesBox.hidden) return;
  if (e.key === "Escape") { e.stopPropagation(); closeSeries(); return; }
  if (e.key !== "Tab") return;
  const stops = [...seriesBox.querySelectorAll("button, input:not(:disabled)")];
  const first = stops[0], last = stops[stops.length - 1];
  if (!seriesBox.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}, true);

/* ---------- действия с записью ----------
   Переименовать, перенести, удалить — хозяину записи. Каждое открывается своим окном:
   у переименования и переноса одно поле, удаление — отдельно, с перечнем того, что
   произойдёт, и красной кнопкой в стороне от «Отмены». Обработка заново не запускается. */
let recAct = null;                  // {kind: "rename" | "move" | "delete" | "mkdir", file, folders, busy}

const baseName = file => file.split("/").pop();
const dirName = file => file.split("/").slice(0, -1).join("/");
const folderLabel = f => f || "Корень (без папки)";

function toast(text) {
  const box = document.getElementById("toast");
  box.textContent = text;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.textContent = ""; }, 5000);
}

function closeRecAct() {
  if (!recAct || recAct.busy) return;
  const kind = recAct.kind;
  recAct = null;
  document.getElementById("recact").hidden = true;
  document.getElementById("recact").innerHTML = "";
  const back = kind === "mkdir" ? document.getElementById("mkbtn")    // фокус — туда, откуда окно открыли
    : document.querySelector(`[data-recact="${kind}"]`);
  if (back) back.focus();
}

function drawRecAct() {
  const { kind, file } = recAct;
  const name = baseName(file || ""), dot = name.lastIndexOf(".");
  const head = (title, cls = "", role = "dialog", more = "") => `<div class="veil" id="raveil">
    <form class="modal ${cls}" id="raform" role="${role}" aria-modal="true" aria-labelledby="ratitle" ${more} novalidate>
    <header><h2 id="ratitle">${title}</h2>
      <button type="button" class="ghost icon" id="raclose" aria-label="Закрыть">${icon("close", 15)}</button></header>
    <div class="body">`;
  const foot = `<div class="msg" id="ramsg" role="alert"></div></div></form></div>`;
  let html;
  if (kind === "mkdir") html = head("Новая папка") + `
      <label for="raname">Название папки</label>
      <input type="text" id="raname" autocomplete="off" maxlength="100" required aria-describedby="ranote">
      <div class="note" id="ranote">${recAct.folder ? `Папка появится внутри «${esc(recAct.folder)}».`
        : "Папка появится в корне записей."} После создания она сразу откроется — в неё можно загружать записи.</div>
      <div class="row"><button id="rago">Создать папку</button>
        <button type="button" class="ghost" id="racancel">Отмена</button></div>` + foot;
  else if (kind === "rename") html = head("Переименовать запись") + `
      <label for="raname">Новое имя записи «${esc(name)}»</label>
      <div class="namerow"><input type="text" id="raname" autocomplete="off" maxlength="200" required
        value="${esc(dot > 0 ? name.slice(0, dot) : name)}" aria-describedby="ranote">
        <span class="dim">${esc(dot > 0 ? name.slice(dot) : "")}</span></div>
      <div class="note" id="ranote">Вместе с записью переименуются протокол, расшифровка и звук. Обработка заново
        не запускается. Ссылки на эту запись, которые кто-то сохранил, перестанут открываться.</div>
      <div class="row"><button id="rago">Переименовать</button>
        <button type="button" class="ghost" id="racancel">Отмена</button></div>` + foot;
  else if (kind === "move") {
    const here = dirName(file), options = recAct.folders.filter(f => f !== here);
    html = head("Перенести запись") + (options.length ? `
      <label for="rafolder">Куда перенести «${esc(name)}»</label>
      <select id="rafolder" aria-describedby="ranote">${options.map(f =>
        `<option value="${esc(f)}">${esc(folderLabel(f))}</option>`).join("")}</select>
      <div class="note" id="ranote">Сейчас запись лежит в папке «${esc(folderLabel(here))}». В списке — папки, где вы
        владелец или администратор. Запись увидят те, кому открыта новая папка. Обработка заново не запускается.</div>
      <div class="row"><button id="rago">Перенести</button>
        <button type="button" class="ghost" id="racancel">Отмена</button></div>` : `
      <p>Переносить некуда: других папок, где вы владелец или администратор, нет.</p>
      <div class="row"><button type="button" class="ghost" id="racancel">Закрыть</button></div>`) + foot;
  } else html = head(`Удалить запись «${esc(name)}»?`, "dangerbox", "alertdialog", `aria-describedby="ralist"`) + `
      <ul class="plain" id="ralist" style="margin-top:0">
        <li>запись исчезнет со страницы у всех, кому открыта папка «${esc(folderLabel(dirName(file)))}»;</li>
        <li>исходный файл, протокол, расшифровка и звук переедут в корзину на сервере и пролежат там
          <b>30 дней</b>, потом сотрутся;</li>
        <li>вернуть запись за это время можно только вручную на сервере — со страницы нельзя;</li>
        <li>расходы на её обработку останутся в отчёте.</li>
      </ul>
      <div class="row apart"><button type="button" class="ghost" id="racancel">Отмена</button>
        <button class="danger" id="rago">Удалить запись</button></div>` + foot;
  const box = document.getElementById("recact");
  box.innerHTML = html;
  box.hidden = false;
  // у удаления фокус на «Отмене»: случайный Enter ничего не сотрёт
  const first = document.getElementById({ rename: "raname", mkdir: "raname", move: "rafolder" }[kind] || "racancel")
    || document.getElementById("racancel");
  first.focus();
  if (first.select) first.select();
}

onRecordAction = async kind => {
  recAct = { kind, file: viewFile, folders: [], busy: false };
  if (kind === "move") {
    try { recAct.folders = (await api("/api/record/folders")).folders; }
    catch (e) { recAct = null; toast("Список папок не получен: " + e.message); return; }
    if (!recAct || recAct.file !== viewFile) return;      // пока ждали, запись закрыли
  }
  drawRecAct();
};

async function runRecAct() {
  const { kind, file } = recAct;
  const go = document.getElementById("rago"), msg = document.getElementById("ramsg");
  const body = { file };
  if (kind === "rename" || kind === "mkdir") {
    const field = document.getElementById("raname");
    body.name = field.value.trim();
    if (!body.name) {
      msg.className = "msg err";
      msg.textContent = kind === "mkdir" ? "впишите название папки" : "впишите имя записи";
      field.focus();
      return;
    }
  }
  const where = recAct.folder;
  if (kind === "move") body.dir = document.getElementById("rafolder").value;
  recAct.busy = true;
  go.disabled = true;
  msg.className = "msg";
  msg.textContent = { rename: "Переименовываю…", move: "Переношу…", delete: "Удаляю…", mkdir: "Создаю…" }[kind];
  let done;
  try {
    done = kind === "mkdir" ? await api("/api/folder", { dir: where ? `${where}/${body.name}` : body.name })
                            : await api(`/api/record/${kind}`, body);
  }
  catch (e) {
    recAct.busy = false;
    go.disabled = false;
    msg.className = "msg err"; msg.textContent = e.message;
    (document.getElementById("raname") || document.getElementById("rafolder") || go).focus();
    return;
  }
  recAct.busy = false;
  closeRecAct();
  if (kind === "mkdir") {
    toast(`Папка «${body.name}» создана`);
    goTo(done.path, true);                // сразу внутрь: туда собирались что-то загрузить
    return;
  }
  if (kind === "delete") {
    // не шаг назад по истории: после переноса он увёл бы в прежнюю папку, а человек удалял в этой
    closeViewer(true);
    history.replaceState(history.state, "", urlFor(folder));
    syncReading();
    toast(`Запись «${baseName(file)}» удалена в корзину на сервере — пролежит там ${done.keep_days} дней`);
  } else {
    // запись живёт по новому пути: открытую папку, адрес и просмотрщик переводим на него
    if (kind === "move" && folder) folder = dirName(done.file);
    await openViewer(done.file, viewKind, true);
    history.replaceState(history.state, "", urlFor(folder, viewFile, viewKind));
    syncReading();
    toast(kind === "rename" ? `Запись переименована: «${baseName(done.file)}»`
                            : `Запись перенесена в папку «${folderLabel(dirName(done.file))}»`);
  }
  load();
}

const recBox = document.getElementById("recact");
recBox.addEventListener("submit", e => { e.preventDefault(); if (!recAct.busy) runRecAct(); });
let recDownOnVeil = false;
recBox.addEventListener("mousedown", e => { recDownOnVeil = e.target.id === "raveil"; });
recBox.addEventListener("click", e => {
  if (e.target.closest("#raclose, #racancel") || (e.target.id === "raveil" && recDownOnVeil)) closeRecAct();
});
// Escape закрывает окно действия, а не запись под ним; Tab ходит по кругу внутри окна
addEventListener("keydown", e => {
  if (!recAct || recBox.hidden) return;
  if (e.key === "Escape") { e.stopPropagation(); closeRecAct(); return; }
  if (e.key !== "Tab") return;
  const stops = [...recBox.querySelectorAll("button:not(:disabled), input, select")];
  const first = stops[0], last = stops[stops.length - 1];
  if (!recBox.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}, true);

// Новая папка — то же окно, что у действий с записью: подпись у поля, ошибка сервера под ним
document.getElementById("mkbtn").onclick = () => {
  recAct = { kind: "mkdir", folder, busy: false };
  drawRecAct();
};
document.getElementById("upbtn").onclick = () => document.getElementById("upfile").click();
document.getElementById("upfile").onchange = e => { addUploads([...e.target.files]); e.target.value = ""; };

const dropzone = document.getElementById("c2");
const hasFiles = e => [...(e.dataTransfer ? e.dataTransfer.types : [])].includes("Files");
// Файл перехватываем и там, где загружать нельзя: иначе браузер открыл бы его вместо страницы,
// а человек так и не узнал бы, почему запись не принята.
dropzone.addEventListener("dragover", e => {
  if (!hasFiles(e) || !data) return;
  e.preventDefault();
  if (uploadDir() === null) { e.dataTransfer.dropEffect = "none"; return; }
  dropzone.classList.add("drop");
});
dropzone.addEventListener("dragleave", e => { if (!dropzone.contains(e.relatedTarget)) dropzone.classList.remove("drop"); });
dropzone.addEventListener("drop", e => {
  if (!hasFiles(e) || !data) return;
  e.preventDefault();
  dropzone.classList.remove("drop");
  if (uploadDir() === null) { refuseDrop(); return; }
  dropNote = "";
  addUploads([...e.dataTransfer.files]);
});

function refuseDrop() {
  dropNote = upFirst(data.upload.denied);
  renderUploads();
}

// закрытая вкладка обрывает загрузку — предупреждаем; продолжить потом можно, но человек об этом не знает
addEventListener("beforeunload", e => {
  if (ups.some(u => u.state === "run" || u.state === "wait")) e.preventDefault();
});

function plural(n, one, few, many) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}


/* ---------- корзина ----------
   Удалённые записи и папки, которые человек может вернуть: запись — хозяин её папки, папку —
   администратор сервиса. «Вернуть» — сразу, без подтверждения: действие обратимо, вернувшееся
   снова удаляют. Итог — строкой под списком (aria-live), отказ — под строкой той записи (role="alert").
   Обработка при возврате не запускается: протокол и расшифровка возвращаются с диска. */
let trashOpen = false, trashItems = [], trashKeep = 30, trashDone = null;

const trashWhere = item => item.dir ? `в папке «${item.dir}»` : "в корне";
const trashBack = item => item.dir ? `в папку «${item.dir}»` : "в корень";

function closeTrash() {
  if (!trashOpen) return;
  trashOpen = false;
  const box = document.getElementById("trash");
  box.hidden = true;
  box.innerHTML = "";
  document.getElementById("trbtn").focus();
}

function drawTrash(focusId) {
  const box = document.getElementById("trash");
  box.hidden = false;
  const rows = trashItems.map((item, i) => `<li class="keyrow">
      <div class="kmain"><b>${esc(item.kind === "folder" ? item.name : (item.title || item.name))}</b>
        <span class="dim">${item.kind === "folder"
          ? `папка, ${item.records} ${plural(item.records, "запись", "записи", "записей")}` : item.title ? esc(item.name) : ""}</span></div>
      <div class="kmeta dim">${esc(trashWhere(item))} · удалено ${ruDay(item.at)} ${esc(item.at.slice(11, 16))}${
        item.by ? ", " + esc(item.by) : ""} · хранится до ${ruDay(item.until)}${item.partial
          ? " · удалено до версии 1.2.0: вернётся без словаря, прав на папку и автора загрузки" : ""}</div>
      <div class="kact"><button type="button" data-restore="${i}" id="trgo${i}">${icon("undo", 15)}<span>Вернуть</span></button></div>
      <div class="msg" id="trmsg${i}"></div>
    </li>`).join("");
  box.innerHTML = `<div class="veil" id="trveil"><div class="modal" role="dialog" aria-modal="true" aria-labelledby="trtitle">
    <header><h2 id="trtitle">Корзина</h2>
      <button class="ghost icon" id="trclose" aria-label="Закрыть">${icon("close", 15)}</button></header>
    <div class="body">
      <p class="klead">Удалённое хранится ${trashKeep} ${plural(trashKeep, "день", "дня", "дней")}, потом стирается само.
        «Вернуть» кладёт запись или папку на прежнее место со всем, что было: протокол, расшифровка, подписи
        говорящих, отметки и сроки поручений, словарь, права на папку. Заново ничего не обрабатывается и не оплачивается.</p>
      ${trashItems.length ? `<ul class="keylist">${rows}</ul>`
        : `<p class="kempty dim">Здесь нет ничего, что вы можете вернуть. Запись возвращает владелец её папки,
           папку — администратор сервиса.</p>`}
      <div class="msg${trashDone ? " " + trashDone.kind : ""}" id="trdone" role="status" aria-live="polite">${
        trashDone ? esc(trashDone.text) : ""}</div>
      <div class="row"><button type="button" class="ghost" id="trok">Закрыть</button></div>
    </div></div></div>`;
  const target = (focusId && document.getElementById(focusId)) || box.querySelector("[data-restore]")
    || document.getElementById("trok");
  target.focus();
}

async function openTrash() {
  trashOpen = true;
  trashDone = null;
  try {
    const d = await api("/api/trash");
    trashItems = d.items;
    trashKeep = d.keep_days;
  } catch (e) {
    trashItems = [];
    trashDone = { kind: "err", text: "Корзина не открылась: " + e.message };
  }
  if (trashOpen) drawTrash();
}

async function restoreFromTrash(i) {
  const item = trashItems[i], btn = document.getElementById("trgo" + i);
  btn.disabled = true;
  btn.lastElementChild.textContent = "Возвращаю…";
  try {
    const d = await api("/api/trash/restore", { id: item.id });
    trashDone = { kind: "ok", text: d.kind === "folder"
      ? `Папка «${item.name}» вернулась ${trashBack(item)}, записей в ней: ${d.records}.`
      : `Запись «${item.title || item.name}» вернулась ${trashBack(item)}.` };
    trashItems.splice(i, 1);
    if (trashOpen) drawTrash();
    load();
  } catch (e) {
    btn.disabled = false;
    btn.lastElementChild.textContent = "Вернуть";
    const msg = document.getElementById("trmsg" + i);
    msg.setAttribute("role", "alert");
    say("trmsg" + i, "err", "Не вернулось: " + e.message);
  }
}

document.getElementById("trbtn").onclick = () => (trashOpen ? closeTrash() : openTrash());
const trashBox = document.getElementById("trash");
let trashDownOnVeil = false;
trashBox.addEventListener("mousedown", e => { trashDownOnVeil = e.target.id === "trveil"; });
trashBox.addEventListener("click", e => {
  if (e.target.closest("#trclose, #trok") || (e.target.id === "trveil" && trashDownOnVeil)) return closeTrash();
  const go = e.target.closest("[data-restore]");
  if (go && !go.disabled) restoreFromTrash(+go.dataset.restore);
});
// Escape закрывает корзину, а не запись под ней; Tab ходит по кругу внутри окна
addEventListener("keydown", e => {
  if (!trashOpen || trashBox.hidden) return;
  if (e.key === "Escape") { e.stopPropagation(); closeTrash(); return; }
  if (e.key !== "Tab") return;
  const stops = [...trashBox.querySelectorAll("button:not(:disabled)")];
  const first = stops[0], last = stops[stops.length - 1];
  if (!trashBox.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
  else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}, true);


document.addEventListener("click", e => {
  const link = e.target.closest("[data-go]");
  if (!link) return;
  e.preventDefault();
  goTo(link.dataset.go, true);
});

/* Пока человек печатает, в API не ходим: запрос уходит через паузу.
   Адрес правим без нового шага истории — иначе «назад» отматывал бы
   запрос по букве. */
document.getElementById("q").addEventListener("input", e => {
  query = e.target.value.trim();
  clearTimeout(qtimer);
  qtimer = setTimeout(() => {
    history.replaceState(history.state, "", urlFor(folder, viewFile, viewKind));
    runSearch();
  }, 350);
});

/* ---------- правка протокола и его версии ----------
   Хозяин папки правит протокол текстом Markdown прямо в окне записи — без модели. Сохранение
   передаёт отпечаток протокола, с которого начата правка (base): протокол за это время поправил
   кто-то ещё или пересобрала модель — сервер отвечает 409, и набранный текст остаётся в поле.
   Прежний текст сервер кладёт в версии протокола; их видно всем, кому открыта запись, а вернуть
   версию может тот же хозяин папки. Несохранённая правка живёт, пока открыта страница: переход
   на другую вкладку или запись её не теряет, а закрыть страницу с ней браузер не даст молча. */
const PROTO_MAX = 200000;           // предел текста правки — как на сервере (summarize.EDIT_MAX_CHARS)
let protoDraft = null;              // {file, text, orig, base} — правка, которая ещё не сохранена
let proto = { file: null, mode: "", list: null, shown: null, ask: false, busy: false };

const protoBody = text => text.replace(/^\s*<!--[\s\S]*?-->\s*/, "");   // без служебной пометки сервиса
const protoDirty = () => !!protoDraft && protoDraft.text !== protoDraft.orig;
const protoWhen = at => `${markDay(at)} ${at.slice(11, 16)}`;
const protoHow = v => v.how === "edit" ? `правка: ${esc(v.by)}` : v.how === "restore" ? `возврат версии: ${esc(v.by)}`
  : "собрала модель";
const protoLive = text => { const live = document.getElementById("vlive"); if (live) { live.className = "tsaid"; live.textContent = text; } };

function protoBar() {
  const e = viewData.edited;
  return `<div class="protobar" role="group" aria-label="Текст протокола">
    <span class="dim">${e ? `${icon("edit", 13)}${e.how === "restore" ? "Возвращена прежняя версия" : "Правили вручную"}: ${
      esc(e.by)}, ${protoWhen(e.at)}` : ""}</span>
    <button type="button" class="ghost copybtn" id="pversions">${icon("list", 13)}Версии</button>
    ${viewData.can_edit ? `<button type="button" class="ghost copybtn" id="pedit" ${viewData.rebuilding ? "disabled" : ""}
      title="${viewData.rebuilding ? "Протокол пересобирается — править можно после сборки" : "Поправить текст протокола без пересборки"}">${icon("edit", 13)}Править</button>` : ""}
  </div>`;
}

function protoEditor() {
  const n = protoDraft.text.length;
  return `<div class="protoed">
    <label for="ptext">Текст протокола в Markdown</label>
    <div class="fhint" id="phint">Заголовки — строки с «#», поручения — строки таблицы «| поручение | срок | в записи | статус |».
      Отметки «выполнено» и сроки останутся у поручений, если исполнитель тот же, а слова почти те же.
      Прежний текст сохранится в версиях протокола.</div>
    <div class="row">
      <button type="button" id="psave">Сохранить</button>
      <button type="button" class="ghost" id="pcancel">Отмена</button>
    </div>
    ${proto.ask ? `<div class="prevbar ask" role="group" aria-labelledby="paskq">
      <p id="paskq"><b>Выбросить правку?</b> Набранный текст не сохранится, протокол останется прежним.</p>
      <div class="row apart"><button type="button" class="ghost" id="paskno">Продолжить правку</button>
        <button type="button" class="danger" id="paskyes">Выбросить правку</button></div></div>` : ""}
    <div class="ferr" id="perr" role="alert"></div>
    <div class="msg" id="pmsg" role="status" aria-live="polite"></div>
    <textarea id="ptext" class="tpltext protoarea" spellcheck="true" autocomplete="off"
      aria-describedby="phint pcount perr">${esc(protoDraft.text)}</textarea>
    <div class="fhint" id="pcount">${protoCount(n)}</div>
  </div>`;
}

const protoCount = n => `${n.toLocaleString("ru")} знаков из ${PROTO_MAX.toLocaleString("ru")}` +
  (n > PROTO_MAX ? " — слишком длинно" : "") + (protoDirty() ? " · есть несохранённая правка" : "");

function protoVersions() {
  const d = proto.list;
  if (!d) return `<div class="protover"><p class="dim" role="status">Загружаю версии протокола…</p></div>`;
  if (d.error) return `<div class="protover"><div class="msg err" role="alert">${esc(d.error)}</div>
    <div class="row"><button type="button" class="ghost" id="pback">К протоколу</button></div></div>`;
  const s = proto.shown;
  return `<div class="protover">
    <div class="row apart"><h3 id="pvh" tabindex="-1">Версии протокола</h3>
      <button type="button" class="ghost copybtn" id="pback">К протоколу</button></div>
    <p class="fhint">Сейчас: ${d.edited ? `${d.edited.how === "restore" ? "возвращённая версия" : "правка"} — ${esc(d.edited.by)},
      ${protoWhen(d.edited.at)}` : "протокол, собранный моделью"}. Прежний текст сохраняется при каждой правке, возврате
      версии и пересборке протокола. Вернуть версию — поставить её текст на место нынешнего; нынешний уйдёт в версии.</p>
    ${d.versions.length ? `<ul class="edvers">${d.versions.map(v => `<li><span>${protoWhen(v.at)}:${v.at.slice(17, 19)}
        · ${protoHow(v)} <span class="dim">· ${v.chars.toLocaleString("ru")} знаков</span></span>
        <span class="rowacts"><button type="button" class="ghost" data-pshow="${esc(v.stamp)}"
          aria-pressed="${!!s && s.stamp === v.stamp}">Показать</button>
          ${d.can_edit ? `<button type="button" class="ghost" data-pask="${esc(v.stamp)}">Вернуть…</button>` : ""}</span></li>`).join("")}</ul>`
      : `<div class="empty">${icon("idle", 17)}прежних версий нет: протокол ещё не правили и не пересобирали</div>`}
    <div class="msg" id="pvmsg" role="alert"></div>
    ${s ? `<section class="pshown" aria-labelledby="psh">
      <h3 id="psh" tabindex="-1">Версия от ${protoWhen(s.at)} — ${protoHow(s)}, только просмотр</h3>
      ${d.can_edit ? `<div class="row"><button type="button" id="prestore">Вернуть эту версию</button>
        <button type="button" class="ghost" id="phide">Скрыть</button></div>` : ""}
      ${s.text === undefined ? `<p class="dim">Загружаю текст версии…</p>` : mdToHtml(s.text)}</section>` : ""}
  </div>`;
}

protocolPane = {
  body() {
    if (proto.file !== viewFile) {           // открыли другую запись: её правка — если она была начата
      proto = { file: viewFile, mode: protoDraft && protoDraft.file === viewFile ? "edit" : "", list: null,
                shown: null, ask: false, busy: false };
    }
    if (proto.mode === "edit" && protoDraft && protoDraft.file === viewFile) return protoEditor();
    if (proto.mode === "versions") return protoVersions();
    return "";
  },
  drawn(doc, own) {
    if (!own) { doc.insertAdjacentHTML("afterbegin", protoBar()); return; }
    const field = document.getElementById("ptext");
    if (field) protoFit(field);
  },
};

// поле — в рост текста: прокручивается окно записи, а не поле внутри него
function protoFit(field) {
  field.style.height = "auto";
  field.style.height = field.scrollHeight + 2 + "px";
}

function protoStart() {
  protoLive("");
  protoDraft = { file: viewFile, text: protoBody(viewData.text), orig: protoBody(viewData.text), base: viewData.version };
  proto.mode = "edit"; proto.ask = false;
  fillDoc();
  const field = document.getElementById("ptext");
  field.focus();
  field.setSelectionRange(0, 0);
  document.querySelector(".doc").scrollTop = 0;
}

function protoClose(text) {
  protoDraft = null;
  proto.mode = ""; proto.ask = false;
  fillDoc();
  if (text) protoLive(text);
  focusIn("#pedit");
}

async function protoSave() {
  const go = document.getElementById("psave"), err = document.getElementById("perr");
  const field = document.getElementById("ptext");
  err.textContent = "";
  field.removeAttribute("aria-invalid");
  if (!protoBody(protoDraft.text).trim()) { err.textContent = "Текст пустой — сохранять нечего"; field.focus(); return; }
  if (protoDraft.text.length > PROTO_MAX) {
    err.textContent = `Текст длиннее ${PROTO_MAX.toLocaleString("ru")} знаков — такой сервис не принимает`;
    field.setAttribute("aria-invalid", "true"); field.focus(); return;
  }
  proto.busy = true;
  go.disabled = true;
  go.textContent = "Сохраняю…";
  say("pmsg", "", "Сохраняю правку…");
  const was = protoDraft;
  try {
    const d = await api("/api/v1/protocol", { file: was.file, text: was.text, base: was.base });
    proto.busy = false;
    if (viewFile !== was.file) { protoDraft = null; return; }       // пока сохранялось, открыли другую запись
    await refresh();
    protoClose(d.changed ? "Протокол сохранён. Прежний текст — в версиях протокола; карта и выгрузки уже с правкой"
                         : "Текст тот же, что в протоколе, — сохранять было нечего");
  } catch (e) {
    proto.busy = false;
    go.disabled = false;
    go.textContent = "Сохранить";
    say("pmsg", "", "");
    // набранное не трогаем: поле остаётся как было, человек решает сам, что с ним делать
    err.innerHTML = esc(e.message) + (e.status === 409
      ? `. Ваш текст остался в поле — скопируйте его, нажмите «Отмена», откройте «Править» заново и перенесите правку
        в новый текст. <button type="button" class="ghost copybtn" id="pcopy">${icon("clip", 13)}Скопировать мой текст</button>`
      : "");
    field.focus();
  }
}

async function protoVersionsOpen(focus) {
  proto.mode = "versions"; proto.list = null; proto.shown = null;
  protoLive("");
  fillDoc();
  const file = viewFile;
  try { proto.list = await api(`/api/v1/versions?file=${encodeURIComponent(file)}`); }
  catch (e) { proto.list = { error: e.message }; }
  if (viewFile !== file || proto.mode !== "versions") return;
  fillDoc();
  focusIn(focus || (proto.list.error ? "#pback" : "#pvh"));
}

async function protoShow(stamp, ask) {
  const v = proto.list.versions.find(x => x.stamp === stamp);
  proto.shown = { ...v, text: undefined };
  fillDoc();
  const file = viewFile;
  try {
    const d = await api(`/api/v1/version?file=${encodeURIComponent(file)}&stamp=${encodeURIComponent(stamp)}`);
    if (viewFile !== file || !proto.shown || proto.shown.stamp !== stamp) return;
    proto.shown.text = d.text;
    fillDoc();
    focusIn(ask ? "#prestore" : "#psh");
  } catch (e) {
    proto.shown = null;
    fillDoc();
    say("pvmsg", "err", e.message);
  }
}

async function protoRestore() {
  const go = document.getElementById("prestore"), s = proto.shown;
  go.disabled = true;
  go.textContent = "Возвращаю…";
  try {
    const d = await api("/api/v1/protocol/restore", { file: viewFile, stamp: s.stamp, base: proto.list.version });
    await refresh();
    proto.mode = ""; proto.shown = null;
    fillDoc();
    protoLive(d.changed ? `Возвращена версия от ${protoWhen(s.at)}. Прежний текст — в версиях протокола: возврат можно отменить`
                        : "Эта версия и так совпадает с нынешним протоколом — ничего не изменилось");
    focusIn("#pversions");
  } catch (e) {
    if (e.status !== 409) {
      go.disabled = false;
      go.textContent = "Вернуть эту версию";
      say("pvmsg", "err", e.message);
      go.focus();
      return;
    }
    // протокол за это время изменился: список версий — заново, с новым отпечатком
    await protoVersionsOpen("#pvh");
    say("pvmsg", "err", e.message + ". Список версий обновлён — посмотрите его ещё раз");
  }
}

document.getElementById("viewer").addEventListener("click", e => {
  const t = e.target;
  if (!viewFile || viewKind !== "summary") return;
  if (t.closest("#pedit")) protoStart();
  else if (t.closest("#pversions")) protoVersionsOpen();
  else if (t.closest("#psave") && !proto.busy) protoSave();
  else if (t.closest("#pcancel")) {
    if (!protoDirty()) { protoClose(""); return; }
    proto.ask = true; fillDoc(); focusIn("#paskno");
  }
  else if (t.closest("#paskno")) { proto.ask = false; fillDoc(); focusIn("#ptext"); }
  else if (t.closest("#paskyes")) protoClose("Правка выброшена, протокол прежний");
  else if (t.closest("#pcopy")) copyText(protoDraft ? protoDraft.text : "").then(ok =>
    say("pmsg", ok ? "ok" : "err", ok ? "Ваш текст скопирован" : "Скопировать не вышло — выделите текст в поле сами"));
  else if (t.closest("#pback")) { proto.mode = ""; proto.shown = null; fillDoc(); focusIn("#pversions"); }
  else if (t.closest("[data-pshow]")) protoShow(t.closest("[data-pshow]").dataset.pshow, false);
  else if (t.closest("[data-pask]")) protoShow(t.closest("[data-pask]").dataset.pask, true);
  else if (t.closest("#phide")) { proto.shown = null; fillDoc(); focusIn("#pvh"); }
  else if (t.closest("#prestore")) protoRestore();
});
document.getElementById("viewer").addEventListener("input", e => {
  if (e.target.id !== "ptext" || !protoDraft) return;
  protoDraft.text = e.target.value;
  protoFit(e.target);
  document.getElementById("pcount").textContent = protoCount(protoDraft.text.length);
});
// Escape в поле правки — «Отмена»: окно записи с набранным текстом не закрывается
document.getElementById("viewer").addEventListener("keydown", e => {
  if (e.target.id === "ptext" && e.key === "Escape") { e.preventDefault(); document.getElementById("pcancel").click(); }
  if (e.target.id === "ptext" && e.key === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (!proto.busy) protoSave(); }
});
// несохранённая правка протокола — браузер переспросит, прежде чем закрыть страницу
addEventListener("beforeunload", e => { if (protoDirty()) e.preventDefault(); });

const start = new URLSearchParams(location.search);
folder = start.get("path") || "";
query = start.get("q") || "";
if (query) { document.getElementById("q").value = query; runSearch(); }
// кто вошёл, нужно знать до первого показа: от этого зависят меню и куда грузить
initUser().then(load).then(() => {
  // прямая ссылка вида /?path=…&open=…&kind=… сразу открывает нужный результат, а с &t=секунды —
  // ещё и ставит запись на этот момент (так ведут ссылки из реестра поручений). Играть без
  // нажатия браузер может не дать — тогда плеер просто стоит на нужном месте.
  if (start.get("open")) openViewer(start.get("open"), start.get("kind") || "summary", true).then(() => {
    syncReading();
    const t = parseInt(start.get("t"), 10);
    if (t >= 0) playFrom(t);
  });
});
setInterval(load, 5000);   // renderSearch сам прячет список папки, пока идёт поиск
