/* Обвязка демонстрации: страницы сервиса работают как есть, а всё, за чем они ходят на сервер,
   отвечает отсюда — из данных, вшитых в файл (DEMO.data, их кладёт tools/demo_page.py).
   Сети нет: чтение отдаётся из данных, любая запись получает отказ с объяснением, и страница
   показывает его там же, где показала бы отказ сервера. Адрес страницы живёт после «#»:
   файл открыт с диска, и другого адреса у него быть не может. */
(function () {
  const D = DEMO.data, real = window.location, realHistory = window.history;
  DEMO.pages = {};

  const READ_ONLY = "в демонстрации изменения не сохраняются";
  const refusals = [
    [/^\/api\/upload/, "в демонстрации загрузка записей отключена"],
    [/^\/api\/record\/send/, "в демонстрации письма не отправляются"],
  ];

  /* ---------- адрес ---------- */
  const parse = v => { const u = new URL(v, "file:///"); return { pathname: u.pathname, search: u.search, hash: u.hash }; };
  const fromHash = () => real.hash.startsWith("#/") ? real.hash.slice(1) : "/";
  let cur = parse(fromHash());
  const location = {
    get pathname() { return cur.pathname; }, get search() { return cur.search; }, get hash() { return cur.hash; },
    get href() { return cur.pathname + cur.search + cur.hash; }, set href(v) { DEMO.go(v); },
  };
  const step = how => (state, title, url) => {
    if (url != null) cur = parse(url);
    realHistory[how]({ demo: 1, state }, "", "#" + location.href);
  };
  const history = {
    get state() { const s = realHistory.state; return s && s.demo ? s.state : null; },
    pushState: step("pushState"), replaceState: step("replaceState"),
    back() { realHistory.back(); }, forward() { realHistory.forward(); },
  };
  // «назад» и «вперёд»: сначала адрес, потом обработчик самой страницы; другая страница — открываем её заново
  window.addEventListener("popstate", e => {
    const next = parse(fromHash());
    if (next.pathname !== cur.pathname) { e.stopImmediatePropagation(); real.reload(); return; }
    cur = next;
  });

  DEMO.go = function (url) {
    if (!DEMO.pages[parse(url).pathname]) { DEMO.say("Этот раздел в демонстрацию не вошёл"); return; }
    if (real.hash !== "#" + url) real.hash = "#" + url;
    real.reload();
  };

  /* ---------- сообщение «в демонстрации недоступно» ---------- */
  let sayTimer = null;
  DEMO.say = function (text) {
    const box = document.getElementById("demo-toast");
    box.textContent = text;
    clearTimeout(sayTimer);
    sayTimer = setTimeout(() => { box.textContent = ""; }, 4500);
  };
  window.alert = text => DEMO.say(String(text));

  /* ---------- ответы вместо сервера ---------- */
  const fold = s => (s || "").toLowerCase().replace(/ё/g, "е");
  const inside = (folder, root) => !root || folder === root || folder.startsWith(root + "/");
  const hasDue = due => !!due && !["не назван", "не определ", "без срока"].some(x => fold(due).startsWith(x));
  const ruDay = d => `${d.slice(8, 10)}.${d.slice(5, 7)}.${d.slice(0, 4)}`;

  // реестр поручений: отбор и порядок — как в app/registry.py
  function tasks(p) {
    const root = p.get("dir") || "", since = p.get("since") || "", until = p.get("until") || "";
    const within = r => inside(r.dir, root) && (!since || r.date >= since) && (!until || r.date <= until);
    const found = D.tasks.filter(within);
    const wanted = fold(p.get("who")).trim(), words = fold(p.get("q")).split(/\s+/).filter(Boolean);
    const picked = found.filter(r => {
      if (wanted && !fold(r.who).includes(wanted)) return false;
      if (p.get("due") === "true" && !(r.due || hasDue(r.due_text))) return false;
      if (p.get("overdue") === "true" && !r.overdue) return false;
      const text = fold([r.task, r.who, r.due_text, r.status, r.title].map(x => x || "").join(" ") + (r.due ? " " + ruDay(r.due) : ""));
      return words.every(w => text.includes(w));
    });
    const still = picked.filter(r => r.open);
    const rank = r => r.due_date ? [r.due ? 0 : 1, r.due_date] : [hasDue(r.due_text) ? 2 : 3, ""];
    const shown = (p.get("open") === "true" ? still : picked).slice();
    if (p.get("sort") === "due") shown.sort((a, b) => { const x = rank(a), y = rank(b); return x[0] - y[0] || x[1].localeCompare(y[1]); });
    else shown.sort((a, b) => b.date.localeCompare(a.date));
    return { tasks: shown, records: D.dated.filter(within).length, total: picked.length, open: still.length,
             people: [...new Set(found.map(r => r.who).filter(Boolean))].sort((a, b) => fold(a).localeCompare(fold(b), "ru")) };
  }

  // поиск по всем записям: все слова запроса, с начала слова — как в app/search.py
  function search(query) {
    const words = fold(query.trim()).slice(0, 200).split(/\s+/).filter(w => w.length >= 2);
    if (!words.length) return { query, words: [], files: [], scanned: 0 };
    const rule = w => new RegExp("(?<![\\p{L}\\p{N}_])" + w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gu");
    const rules = words.map(rule), longest = rule(words.reduce((a, b) => b.length > a.length ? b : a));
    const CONTEXT = 110, PER_FILE = 3, squeeze = s => s.split(/\s+/).filter(Boolean).join(" ");
    const files = [];
    for (const rec of D.search) {
      const hits = {}, pieces = [];
      for (const kind of ["summary", "triage", "transcript"]) {
        const text = rec.texts[kind], folded = fold(text);
        if (!rules.every(rx => { rx.lastIndex = 0; return rx.test(folded); })) continue;
        const found = [...folded.matchAll(longest)];
        hits[kind] = found.length;
        for (const m of found.slice(0, PER_FILE)) {
          let i = m.index, j = i + m[0].length;
          while (j < folded.length && /[\p{L}\p{N}]/u.test(folded[j])) j++;
          pieces.push({ kind, before: (i > CONTEXT ? "…" : "") + squeeze(text.slice(Math.max(0, i - CONTEXT), i)),
                        match: text.slice(i, j), after: squeeze(text.slice(j, j + CONTEXT)) + (j + CONTEXT < text.length ? "…" : "") });
        }
      }
      const kinds = Object.keys(hits);
      if (kinds.length) files.push({ file: rec.file, name: rec.file.split("/").pop(), dir: rec.dir, finished_at: rec.finished_at,
        hits: kinds.reduce((n, k) => n + hits[k], 0), kinds: hits,
        open_kind: kinds.length === 1 && kinds[0] === "transcript" ? "transcript" : "summary", snippets: pieces.slice(0, PER_FILE) });
    }
    files.sort((a, b) => b.hits - a.hits || b.finished_at.localeCompare(a.finished_at));
    return { query, words, scanned: D.search.length, files, more: 0 };
  }

  const no = (status, detail) => [status, { detail }];
  function answer(method, path, p) {
    const refusal = refusals.find(([rx]) => rx.test(path));
    if (refusal) return no(403, refusal[1]);
    if (method !== "GET") return no(403, READ_ONLY);
    const file = p.get("file") || "";
    switch (path) {
      case "/api/brand": return [200, { name: "", logo: null }];
      case "/api/me": return [200, D.me];
      case "/api/browse": {
        const got = D.browse[p.get("flat") === "true" ? "*" : (p.get("path") || "").replace(/^\/+|\/+$/g, "")];
        return got ? [200, got] : no(404, "такой папки нет");
      }
      case "/api/result": {
        const got = (D.results[file] || {})[p.get("kind") || "summary"];
        return got ? [200, got] : no(404, "такого результата нет");
      }
      case "/api/search": return (p.get("q") || "").trim().length < 2 ? no(400, "слишком короткий запрос") : [200, search(p.get("q"))];
      case "/api/v1/tasks": return [200, tasks(p)];
      case "/api/v1/tree": return [200, D.tree];
      case "/api/record/folders": return [200, { folders: D.own }];
      case "/api/record/recipients": return [200, D.recipients];
      case "/api/dictionary": {
        const got = D.dictionaries[file ? "file:" + file : "path:" + (p.get("path") || "")];
        return got ? [200, got] : no(404, "такой папки нет");
      }
      case "/api/access": return D.access[p.get("dir")] ? [200, D.access[p.get("dir")]] : no(403, "раздавать доступ к этой папке вы не можете");
    }
    return no(404, "в демонстрации этого нет");
  }

  window.fetch = function (input, init) {
    const url = new URL(typeof input === "string" ? input : input.url, "file:///");
    // наружу из файла не уходит ничего: всё, что не адрес сервиса, — отказ без запроса
    if (url.protocol !== "file:" || !url.pathname.startsWith("/api/")) return Promise.reject(new TypeError("нет сети"));
    const [status, body] = answer(((init && init.method) || "GET").toUpperCase(), url.pathname, url.searchParams);
    return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  };

  /* ---------- карта записи: в сервисе — отдельная страница, здесь — поверх этой ---------- */
  let mapBack = null;
  function openMap(file) {
    const html = D.maps[file];
    if (!html) { DEMO.say("У этой записи карты нет"); return; }
    mapBack = document.activeElement;
    const box = document.getElementById("demo-map");
    box.querySelector("iframe").srcdoc = html;
    box.hidden = false;
    document.getElementById("demo-map-close").focus();
  }
  function closeMap() {
    const box = document.getElementById("demo-map");
    if (box.hidden) return;
    box.hidden = true;
    box.querySelector("iframe").removeAttribute("srcdoc");
    if (mapBack && mapBack.isConnected) mapBack.focus();
  }

  /* ---------- ссылки ----------
     Ссылки сервиса ведут на адреса сервера. Папки и записи страница открывает сама; остальное
     разбираем здесь: другая страница файла, карта — либо объяснение, почему не откроется. */
  function onLink(e) {
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a) return;
    const href = a.getAttribute("href");
    if (!href.startsWith("/")) return;
    e.preventDefault();
    if (e.type !== "click") return;
    if (a.hasAttribute("data-go") || a.classList.contains("open")) return;      // это делает сама страница
    const url = new URL(href, "file:///");
    if (url.pathname === "/api/map" && !url.searchParams.get("download")) return openMap(url.searchParams.get("file"));
    if (url.pathname.startsWith("/api/")) return DEMO.say("Скачивание файлов в демонстрации недоступно");
    DEMO.go(href);
  }
  document.addEventListener("click", onLink, true);
  document.addEventListener("auxclick", onLink, true);
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !document.getElementById("demo-map").hidden) { e.stopPropagation(); closeMap(); }
  }, true);

  DEMO.noAudio = () => DEMO.say("Звук в демонстрацию не вложен. В сервисе метка времени включает запись с этого места.");

  DEMO.start = function () {
    if (!DEMO.pages[cur.pathname]) cur = parse("/");
    const page = document.getElementById("demo-page:" + cur.pathname);
    document.title = page.dataset.title;
    document.getElementById("demo-bar").after(page.content.cloneNode(true));
    document.getElementById("demo-map-close").addEventListener("click", closeMap);
    // модальные окна сервиса занимают весь экран — отступаем на высоту полоски, чтобы она оставалась видна
    const bar = document.getElementById("demo-bar");
    const fit = () => document.documentElement.style.setProperty("--demo-h", bar.offsetHeight + "px");
    fit();
    window.addEventListener("resize", fit);
    DEMO.pages[cur.pathname](location, history);
  };
})();
