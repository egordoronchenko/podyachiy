/* Администрирование: «Настройки» — шаблоны протокола, задания модели и словарь. */
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

// общие правила: у общего файла — кто его куски подключает, у шаблона — какие куски он берёт оттуда
function tplCommonNote(t) {
  const common = tpl.items.find(i => i.kind === "common");
  if (t.kind === "common") {
    const users = tpl.items.filter(i => i.includes.length);
    return `<div class="nhint">${icon("info", 14)}<span>${users.length
      ? `Куски этого файла подключают: ${users.map(i => `${esc(i.title)} (${i.includes.map(esc).join(", ")})`).join("; ")}.
        Правка куска действует во всех этих шаблонах со следующей записи; кусок, который подключают, сервис убрать не даст.`
      : `Ни один шаблон куски этого файла не подключает: правка здесь пока ни на что не влияет.`}</span></div>`;
  }
  if (!t.includes.length) return "";
  return `<div class="nhint">${icon("info", 14)}<span>Строки «@@ имя» — общие правила: при сборке задания на их место
    встают куски ${t.includes.map(n => `«${esc(n)}»`).join(", ")} из файла «${esc(common ? common.title : "Общие правила шаблонов")}».
    Модель получает шаблон уже с ними.</span></div>`;
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
    ${tplCommonNote(t)}
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
