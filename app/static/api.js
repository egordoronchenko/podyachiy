initSidebar("api");
initTheme();
ICONS.link = '<path d="M10 13a5 5 0 0 0 7.1 0l3-3a5 5 0 0 0-7.1-7.1l-1.1 1.1"/><path d="M14 11a5 5 0 0 0-7.1 0l-3 3a5 5 0 0 0 7.1 7.1l1.1-1.1"/>';
ICONS.send = '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>';

let spec = null, ops = [], shell = "bash";
const TYPES = { string: "строка", integer: "целое число", number: "число", boolean: "true или false", array: "список",
                object: "объект", null: "null" };
const slug = n => "intro-" + n;

// Текст описаний — Markdown того же вида, что протоколы: разбирает общий mdToHtml из common.js.
const md = text => mdToHtml(text || "");

function typeOf(schema) {
  const s = schema || {};
  const kinds = s.anyOf ? s.anyOf.map(a => a.type) : s.type ? [s.type] : [];
  const named = kinds.filter(k => k !== "null").map(k => TYPES[k] || k).join(" или ") || "любое";
  const def = s.default;
  return named + (def === undefined || def === null || def === "" ? "" : `, по умолчанию ${def}`);
}

function refOf(schema) {
  const name = schema && schema.$ref ? schema.$ref.split("/").pop() : "";
  return name ? spec.components.schemas[name] : null;
}

function table(head, rows) {
  return `<table class="fold"><thead><tr>${head.map(h => `<th scope="col">${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.join("")}</tbody></table>`;
}

// описание поля — одна строка Markdown; абзац вокруг неё в ячейке таблицы не нужен
const cell = text => md(text).replace(/^<p>|<\/p>$/g, "");

function paramRows(o) {
  return (o.parameters || []).map(p => `<tr><td class="n">${esc(p.name)}${
    p.required ? ' <span class="req">обязателен</span>' : ""}</td><td class="t">${esc(typeOf(p.schema))}</td>
    <td>${cell(p.description)}</td></tr>`);
}

function bodyBlock(o) {
  const body = o.requestBody;
  if (!body) return "";
  if (body.content["application/octet-stream"])
    return `<h4>Тело запроса</h4><p>${cell(body.description)}. Тип — <code>application/octet-stream</code>, без частей.</p>`;
  const media = body.content["application/json"], model = refOf(media.schema) || { properties: {} };
  const need = new Set(model.required || []);
  const rows = Object.entries(model.properties).map(([name, p]) => `<tr><td class="n">${esc(name)}${
    need.has(name) ? ' <span class="req">обязательно</span>' : ""}</td><td class="t">${esc(typeOf(p))}</td>
    <td>${cell(p.description)}</td></tr>`);
  return `<h4>Тело запроса — JSON</h4>${table(["Поле", "Тип", "Что значит"], rows)}
    ${media.example === undefined ? "" : `<pre tabindex="0" aria-label="Пример тела запроса">${esc(JSON.stringify(media.example, null, 2))}</pre>`}`;
}

function answerBlock(o) {
  const [type, good] = Object.entries(o.responses["200"].content)[0];
  const json = type === "application/json";
  const fields = Object.entries((good.schema && good.schema.properties) || {});
  const text = json ? JSON.stringify(good.example, null, 2) : String(good.example);
  return `<h4>Ответ${json ? "" : ` — <code>${esc(type)}</code>`}</h4>
    ${fields.length ? table(["Поле", "Тип", "Что значит"], fields.map(([name, p]) =>
      `<tr><td class="n">${esc(name)}</td><td class="t">${esc(TYPES[p.type] || p.type)}</td><td>${cell(p.description)}</td></tr>`)) : ""}
    <h4>Пример ответа</h4><pre tabindex="0" aria-label="Пример ответа">${esc(text)}</pre>`;
}

function refusalBlock(o) {
  const rows = Object.entries(o.responses).filter(([code]) => code !== "200").flatMap(([code, a]) =>
    (a["x-refusals"] || []).map(r => `<tr><td class="n">${esc(code)}</td>
      <td>«${esc(r.detail)}${/[ :]$/.test(r.detail) ? "…" : ""}»</td>
      <td>${cell(r.when)}</td></tr>`));
  return rows.length ? `<h4>Отказы</h4>${table(["Код", "Текст в <code>detail</code>", "Когда"], rows)}` : "";
}

// «Выполнить» — только безопасное чтение: GET, бесплатный, ответ текстом или JSON.
const tryable = o => o.method === "GET" && !o["x-paid"] &&
  ["application/json", "text/markdown"].includes(Object.keys(o.responses["200"].content)[0]);

function tryBlock(o) {
  if (!tryable(o)) return "";
  const fields = (o.parameters || []).map(p => `<div><label for="t-${o.operationId}-${esc(p.name)}">${esc(p.name)}</label>
    <input type="text" id="t-${o.operationId}-${esc(p.name)}" data-param="${esc(p.name)}" autocomplete="off"
           value="${esc(p.example === undefined ? "" : p.example)}"></div>`).join("");
  return `<details class="try"><summary>${icon("send", 15)}<span>Попробовать: выполнить этот запрос</span></summary>
    <form data-try="${o.operationId}" novalidate>
      ${fields ? `<div class="fields">${fields}</div>` : ""}
      <div class="row"><button type="submit">Выполнить</button></div>
      <div class="msg" role="status" aria-live="polite"></div>
      <pre tabindex="0" aria-label="Ответ сервиса" hidden></pre>
    </form></details>`;
}

function opCard(o) {
  const samples = o["x-codeSamples"], paid = o["x-paid"];
  return `<section class="card op" id="${o.operationId}" aria-labelledby="h-${o.operationId}">
    <header>
      <span class="verb ${o.method.toLowerCase()}">${o.method}</span><span class="addr">${esc(o.path)}</span>
      <a class="anchor" href="#${o.operationId}" aria-label="Ссылка на метод ${o.method} ${esc(o.path)}" title="Ссылка на этот метод">${icon("link", 15)}</a>
      <h3 id="h-${o.operationId}">${esc(o.summary)}</h3>
    </header>
    <div class="badges">
      <span class="badge2">${icon("users", 13)}<span>Кому: ${cell(o["x-rights"].text)}</span></span>
      ${o["x-key"] === "manage" ? `<span class="badge2 manage">${icon("key", 13)}<span>Ключ «с управлением» или логин с паролем</span></span>` : ""}
      ${o["x-key"] === "none" && !personal() ? `<span class="badge2">${icon("ok", 13)}<span>Без входа</span></span>` : ""}
      ${paid ? `<span class="badge2 paid">${icon("coin", 13)}<span>Платный</span></span>`
             : `<span class="badge2">Бесплатный</span>`}
    </div>
    ${paid ? `<p class="note">${icon("coin", 14)}<span><b>Стоит денег</b> — ${cell(o["x-paid-when"])}.</span></p>` : ""}
    ${md(o.description)}
    ${(o.parameters || []).length ? `<h4>Параметры адреса</h4>${table(["Параметр", "Тип", "Что значит"], paramRows(o))}` : ""}
    ${bodyBlock(o)}
    <h4>Пример запроса</h4>
    <div class="codebar">
      <div class="tabs" role="tablist" aria-label="Оболочка">
        ${samples.map(s => `<button type="button" role="tab" data-shell="${s.lang}" aria-selected="${s.lang === shell}">${esc(s.label)}</button>`).join("")}
      </div>
      <span class="msg" role="status" aria-live="polite"></span>
      <button type="button" class="ghost copybtn" data-copy="${o.operationId}">${icon("clip", 13)}<span>Скопировать пример</span></button>
    </div>
    ${samples.map(s => `<pre class="wrap" tabindex="0" data-code="${s.lang}" ${s.lang === shell ? "" : "hidden"}>${esc(s.source)}</pre>`).join("")}
    ${answerBlock(o)}
    ${refusalBlock(o)}
    ${tryBlock(o)}
  </section>`;
}

const personal = () => !spec.security;

function draw() {
  const base = spec.servers[0].url;
  document.getElementById("apibase").textContent = base;
  const phrase = `Прочитай описание API Подьячего по адресу ${base}/api/v1/openapi.json и работай с сервисом по нему`;
  document.getElementById("aiphrase").textContent = phrase;
  const copy = document.getElementById("aicopy");
  copy.innerHTML = icon("clip", 15) + "<span>Скопировать фразу</span>";
  copy.onclick = async () => say("aimsg", ...(await copyText(phrase) ? ["ok", "Скопировано — вставьте в чат клиента."]
    : ["err", "Не удалось скопировать — выделите текст и скопируйте вручную."]));
  document.getElementById("keybox").hidden = personal();        // личный режим: входа нет, ключ не нужен

  ops = Object.entries(spec.paths).flatMap(([path, methods]) =>
    Object.entries(methods).map(([method, o]) => ({ ...o, path, method: method.toUpperCase() })));
  for (const o of ops)        // по чему ищет фильтр: адрес, название, описание, параметры и поля
    o.hay = [o.method, o.path, o.summary, o.description, o.tags[0], o["x-rights"].text,
             ...(o.parameters || []).map(p => p.name + " " + p.description)].join(" ").toLowerCase().replace(/ё/g, "е");

  // вступление: разделы «##» описания — отдельными карточками с якорями
  const parts = spec.info.description.split(/\n(?=## )/);
  const intro = parts.map((part, n) => {
    const title = n ? part.match(/^## (.*)/)[1] : "Как это работает";
    return { id: slug(n), title, html: `<section class="card intro" id="${slug(n)}"><h2>${esc(title)}</h2>${
      md(n ? part.replace(/^## .*\n/, "") : part)}</section>` };
  });

  document.getElementById("apimain").innerHTML = intro.map(i => i.html).join("") + spec.tags.map((tag, n) =>
    `<div class="group" id="group-${n}" data-group><h2>${esc(tag.name)}</h2><p>${cell(tag.description)}</p>${
      ops.filter(o => o.tags[0] === tag.name).map(opCard).join("")}</div>`).join("");

  document.getElementById("tocnav").innerHTML =
    `<h3 data-intro>Вступление</h3>` + intro.map(i => `<a href="#${i.id}" data-intro>${esc(i.title)}</a>`).join("") +
    spec.tags.map((tag, n) => `<h3 data-for="group-${n}">${esc(tag.name)}</h3>` + ops.filter(o => o.tags[0] === tag.name).map(o =>
      `<a href="#${o.operationId}" data-op="${o.operationId}" title="${esc(o.summary)}"><span class="verb ${o.method.toLowerCase()}">${o.method}</span><span class="p">${
        esc(o.path.replace(/^\/api\/(v1\/?)?/, "") || "v1")}</span></a>`).join("")).join("");
  document.getElementById("tocsum").innerHTML = icon("list", 16) + `<span>Содержание: ${ops.length} методов</span>`;
  document.getElementById("apifoot").innerHTML = `Подьячий ${esc(spec.info.version)} · <a href="/help#ai">Справка: интеграция с ИИ</a>`;
  filter();
  watchScroll();
  if (location.hash.length > 1) jump(location.hash.slice(1));
}

function filter() {
  const words = document.getElementById("apifilter").value.toLowerCase().replace(/ё/g, "е").split(/\s+/).filter(Boolean);
  let shown = 0;
  for (const o of ops) {
    const hit = words.every(w => o.hay.includes(w));
    shown += hit;
    document.getElementById(o.operationId).hidden = !hit;
    document.querySelector(`#tocnav [data-op="${o.operationId}"]`).hidden = !hit;
  }
  for (const group of document.querySelectorAll("[data-group]")) {
    group.hidden = !group.querySelector(".op:not([hidden])");
    document.querySelector(`#tocnav [data-for="${group.id}"]`).hidden = group.hidden;
  }
  // пока ищут метод, вступление не мешает
  for (const el of document.querySelectorAll(".intro, #tocnav [data-intro]")) el.hidden = words.length > 0;
  document.getElementById("apiempty").hidden = shown > 0;
  document.getElementById("apicount").textContent = words.length ? `Показано методов: ${shown} из ${ops.length}` : `Методов: ${ops.length}`;
}

function jump(id) {
  const target = document.getElementById(id);
  if (!target) return;
  if (target.hidden) { document.getElementById("apifilter").value = ""; filter(); }      // ссылка на метод важнее фильтра
  target.scrollIntoView();
}

// какой раздел сейчас на экране — подсвечен в содержании
function watchScroll() {
  const links = new Map([...document.querySelectorAll("#tocnav a")].map(a => [a.getAttribute("href").slice(1), a]));
  const seen = new Set();
  const watcher = new IntersectionObserver(entries => {
    for (const e of entries) e.isIntersecting ? seen.add(e.target.id) : seen.delete(e.target.id);
    const first = [...links.keys()].find(id => seen.has(id));
    for (const [id, a] of links) {
      a.classList.toggle("on", id === first);
      if (id === first) a.setAttribute("aria-current", "location"); else a.removeAttribute("aria-current");
    }
    // содержание длиннее экрана: подсвеченная строка остаётся на виду (сама страница при этом не двигается)
    const on = links.get(first), toc = document.getElementById("toc");
    if (on && toc.scrollHeight > toc.clientHeight) {
      const top = on.offsetTop;           // содержание «прилипает» к экрану — отсчёт от него самого
      if (top < toc.scrollTop + 30 || top > toc.scrollTop + toc.clientHeight - 60) toc.scrollTop = top - toc.clientHeight / 3;
    }
  }, { rootMargin: "-10% 0px -70% 0px" });
  for (const id of links.keys()) watcher.observe(document.getElementById(id));
}

async function runTry(form) {
  const o = ops.find(x => x.operationId === form.dataset.try);
  const msg = form.querySelector(".msg"), out = form.querySelector("pre"), go = form.querySelector("button");
  const key = document.getElementById("apikey").value.trim();
  if (o["x-key"] !== "none" && !key) {
    msg.className = "msg err";
    msg.textContent = "Впишите ключ доступа в поле вверху страницы — запрос уйдёт с ним.";
    document.getElementById("apikey").focus();
    return;
  }
  const query = new URLSearchParams();
  for (const input of form.querySelectorAll("[data-param]")) if (input.value !== "") query.set(input.dataset.param, input.value);
  const url = o.path + (query.toString() ? "?" + query : "");
  go.disabled = true;
  msg.className = "msg";
  msg.textContent = "Выполняю…";
  try {
    // без куки страницы: запрос идёт только с ключом из поля — так, как его выполнит программа
    const r = await fetch(url, { credentials: "omit", cache: "no-store",
                                 headers: o["x-key"] === "none" ? {} : { Authorization: "Bearer " + key } });
    let text = await r.text();
    try { text = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { /* не JSON — как есть */ }
    const cut = 20000;
    out.textContent = text.length > cut ? text.slice(0, cut) + `\n… ещё ${text.length - cut} знаков` : text;
    out.hidden = false;
    msg.className = "msg " + (r.ok ? "ok" : "err");
    msg.textContent = `${r.ok ? "Ответ" : "Отказ"} ${r.status} · GET ${decodeURI(url)}`;
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = "Сервис не ответил: " + e.message;
  }
  go.disabled = false;
}

document.getElementById("apifilter").addEventListener("input", filter);
document.addEventListener("submit", e => {
  const form = e.target.closest("form[data-try]");
  if (form) { e.preventDefault(); runTry(form); }
});
document.addEventListener("click", async e => {
  const tab = e.target.closest("[data-shell]");
  if (tab) {                // оболочку выбирают один раз — для всех примеров страницы
    const top = tab.getBoundingClientRect().top;
    shell = tab.dataset.shell;
    for (const b of document.querySelectorAll("[data-shell]")) b.setAttribute("aria-selected", b.dataset.shell === shell);
    for (const pre of document.querySelectorAll("[data-code]")) pre.hidden = pre.dataset.code !== shell;
    // примеры разной высоты: возвращаем нажатую вкладку на прежнее место экрана
    const scroller = document.getElementById("apipage");
    (scroller.scrollHeight > scroller.clientHeight ? scroller : document.scrollingElement)
      .scrollTop += tab.getBoundingClientRect().top - top;
    return;
  }
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    const card = document.getElementById(copy.dataset.copy), msg = card.querySelector(".codebar .msg");
    const ok = await copyText(card.querySelector(`[data-code="${shell}"]`).textContent);
    msg.className = "msg " + (ok ? "ok" : "err");
    msg.textContent = ok ? "Скопировано" : "Не удалось — выделите текст вручную";
    setTimeout(() => { msg.textContent = ""; }, 2500);
    return;
  }
  const link = e.target.closest('a[href^="#"]');
  if (link && link.closest(".apidoc")) {
    e.preventDefault();
    const id = link.getAttribute("href").slice(1);
    history.replaceState(null, "", "#" + id);
    jump(id);
    if (link.classList.contains("anchor")) copyText(location.href);          // значок якоря заодно копирует ссылку
    if (matchMedia("(max-width: 900px)").matches && link.closest("#toc")) document.getElementById("toc").open = false;
  }
});
window.addEventListener("hashchange", () => jump(location.hash.slice(1)));
// на узком экране содержание свёрнуто, пока его не открыли
const narrow = matchMedia("(max-width: 900px)");       // на узком экране содержание свёрнуто, на широком — всегда раскрыто
const tocFits = () => { document.getElementById("toc").open = !narrow.matches; };
narrow.addEventListener("change", tocFits);
tocFits();

// Страница открыта и без входа: описание не секрет. Меню слева — как у справки.
initUser().then(() => { if (me && (me.login || me.personal)) loadSidebar(); else renderNav([]); });
fetch("/api/v1/openapi.json", { cache: "no-store" }).then(r => r.json()).then(s => { spec = s; draw(); }).catch(err => {
  document.getElementById("apimain").innerHTML = `<div class="card"><div class="msg err" role="alert">Описание не загрузилось: ${esc(err.message)}.
    Обновите страницу.</div></div>`;
});
