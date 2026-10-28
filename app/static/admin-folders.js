/* Администрирование: «Папки и доступ». */
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
