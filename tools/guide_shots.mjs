// Снимки экрана для руководства пользователя (docs/guide/shots/*.webp).
//
// Снимаются с одноразового стенда на выдуманных данных — никогда с рабочей установки:
//
//   1. Образ под отдельным именем и пустые папки данных:
//        docker build -t podyachiy-docs-test:local .
//        mkdir стенд/inbox стенд/outbox стенд/state стенд/prompts
//   2. Первый запуск (сервис кладёт шаблоны в prompts), корпоративный режим, свободный порт,
//      свой одноразовый пароль администратора, заглушка вместо ключа поставщика и почтового сервера
//      (почта «включена», чтобы у записи была кнопка «Отправить»; писем стенд не шлёт):
//        docker run -d --name podyachiy-docs-test -p 127.0.0.1:8098:8080 \
//          -e MODE=corporate -e OPENAI_API_KEY=stub -e OPENAI_BASE_URL=http://127.0.0.1:9/v1 \
//          -e ADMIN_PASSWORD=<одноразовый> -e PUBLIC_URL=http://127.0.0.1:8098 \
//          -e SMTP_HOST=127.0.0.1 -e SMTP_PORT=9 -e SMTP_SECURITY=none -e SMTP_FROM=podyachiy@example.ru \
//          -v стенд/inbox:/data/inbox -v стенд/outbox:/data/outbox -v стенд/state:/data/state \
//          -v стенд/prompts:/app/prompts podyachiy-docs-test:local
//   3. Остановить, наполнить выдуманными данными (tools/demo_seed.py — как в его описании; входы
//      он запишет в файл), запустить снова:
//        docker stop podyachiy-docs-test
//        docker run --rm -e PYTHONPATH=/app -v <репозиторий>/tools:/tools:ro -v стенд:/install \
//          -v …те же четыре папки… podyachiy-docs-test:local python /tools/demo_seed.py /install/logins.md
//        docker start podyachiy-docs-test
//   4. Снять (нужны Node.js 22+ и Chrome; библиотек не нужно):
//        node tools/guide_shots.mjs http://127.0.0.1:8098 стенд/logins.md docs/guide/shots
//   5. Просмотреть каждый снимок глазами: только выдуманные люди и данные, ни паролей, ни значений
//      ключей. Затем пересобрать документы: python tools/docs_build.py
//   6. Удалить контейнер, образ и папку стенда.
//
// Светлая тема, окно 1280 точек, обычная плотность, WebP. Даты демонстрационных данных привязаны
// к 10.10.2026: в другой день «просрочено» в реестре поручений будет выглядеть иначе.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [base, loginsFile, outDir = "docs/guide/shots", only = ""] = process.argv.slice(2);
if (!base || !loginsFile) {
  console.error("запуск: node tools/guide_shots.mjs <адрес стенда> <файл входов от demo_seed.py> [папка снимков] [имя одного снимка]");
  process.exit(2);
}
const CHROME = process.env.CHROME || ["C:/Program Files/Google/Chrome/Application/chrome.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"].find(existsSync);
const WIDTH = 1280, QUALITY = 82;
const MEETING = "Снабжение/Планёрки/2026-10-05 Планёрка снабжения.m4a";
const open = (file, kind = "summary") => "/?" + new URLSearchParams({ path: file.slice(0, file.lastIndexOf("/")), open: file, kind });
const MASK_KEYS = `{ const walk = document.createTreeWalker(document.getElementById("keys"), NodeFilter.SHOW_TEXT);
  for (let node; (node = walk.nextNode());) node.nodeValue = node.nodeValue.replace(/stg_[^\s]+/g, "stg_••••••••"); }`;
const click = sel => `document.querySelector(${JSON.stringify(sel)}).click()`;

// имя файла, под кем, адрес, высота окна, что сделать на странице перед снимком
const SHOTS = [
  ["main", "m.orlova", open(MEETING), 800, []],
  ["upload", "m.orlova", "/?" + new URLSearchParams({ path: "Снабжение/Планёрки" }), 620, []],
  ["tasks-in-protocol", "m.orlova", open(MEETING), 760,
    [`[...document.querySelectorAll(".doc h1, .doc h2, .doc h3")].find(h => /Задачи/.test(h.textContent)).scrollIntoView()`,
     // прокрутиться должен только текст протокола, а не вся страница
     `for (let el = document.querySelector(".doc").parentElement; el; el = el.parentElement) el.scrollTop = 0; scrollTo(0, 0)`]],
  ["transcript", "m.orlova", open(MEETING, "transcript"), 800, [click("#vnames")]],
  ["dictionary", "m.orlova", "/?" + new URLSearchParams({ path: "Снабжение" }), 800, [click("#dcbtn")]],
  ["search", "m.orlova", "/?" + new URLSearchParams({ q: "стрейч-плёнка" }), 620, []],
  ["tasks", "m.orlova", "/tasks?" + new URLSearchParams({ dir: "Снабжение" }), 800, []],
  ["map", "m.orlova", "/api/map?" + new URLSearchParams({ file: MEETING }), 800, []],
  ["download", "m.orlova", open(MEETING), 520, [`document.getElementById("vdl").open = true`]],
  ["send", "m.orlova", open(MEETING), 800, [click("#vsend")]],
  ["access", "m.orlova", "/?" + new URLSearchParams({ path: "Снабжение" }), 700, [click("#acbtn")]],
  // начало ключа в списке закрыто: значений ключей на снимках быть не должно, даже одноразовых
  ["keys", "m.orlova", "/", 800, [click("#keysbtn"), MASK_KEYS]],
  ["admin-users", "a.melnikov", "/admin#users", 900, []],
  ["admin-costs", "a.melnikov", "/admin#costs", 1000, []],
];

// таблица входов — до раздела с ключами доступа: в нём те же логины, но в последней колонке ключ
const passwords = Object.fromEntries([...readFileSync(loginsFile, "utf8").split("## ")[0].matchAll(/^\| ([\w.]+) \|.*\| `([^`]+)` \|$/gm)]
  .map(m => [m[1], m[2]]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const port = 9400 + Math.floor(Math.random() * 400);
const profile = mkdtempSync(join(tmpdir(), "guide-shots-"));
const chrome = spawn(CHROME, ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--lang=ru-RU",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
let target;
for (let i = 0; i < 50 && !target; i++) {
  await sleep(200);
  try { target = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).find(t => t.type === "page"); } catch { /* ещё поднимается */ }
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
let seq = 0;
const waiting = new Map();
ws.addEventListener("message", e => {
  const m = JSON.parse(e.data);
  if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id); }
});
const send = (method, params = {}) => new Promise((done, fail) => {
  const id = ++seq;
  waiting.set(id, m => m.error ? fail(new Error(`${method}: ${m.error.message}`)) : done(m.result));
  ws.send(JSON.stringify({ id, method, params }));
});
const run = async expression => {
  const got = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (got.exceptionDetails) throw new Error(`на странице: ${got.exceptionDetails.exception?.description || got.exceptionDetails.text}\n  ${expression}`);
  return got.result.value;
};

await send("Page.enable");
await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
let user = "";
try {
  for (const [name, who, path, height, steps] of SHOTS) {
    if (only && only !== name) continue;
    await send("Emulation.setDeviceMetricsOverride", { width: WIDTH, height, deviceScaleFactor: 1, mobile: false });
    if (who !== user) {
      await send("Page.navigate", { url: base + "/login" });
      await sleep(800);
      const status = await run(`fetch("/api/logout", { method: "POST" }).then(() => fetch("/api/login", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: ${JSON.stringify(JSON.stringify({ login: who, password: passwords[who] }))} }))
        .then(r => r.status)`);
      if (status !== 200) throw new Error(`вход под ${who} не удался: ${status}`);
      user = who;
    }
    await send("Page.navigate", { url: "about:blank" });
    await send("Page.navigate", { url: base + path });
    await sleep(1800);
    for (const step of steps) { await run(step); await sleep(900); }
    await run(`document.activeElement && document.activeElement.blur()`);
    await sleep(200);
    const shot = await send("Page.captureScreenshot", { format: "webp", quality: QUALITY });
    writeFileSync(join(outDir, name + ".webp"), Buffer.from(shot.data, "base64"));
    console.log(`${name}.webp — ${Math.round(shot.data.length * 3 / 4 / 1024)} КБ`);
  }
} finally {
  ws.close();
  chrome.kill();
  await sleep(500);
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* профиль браузера ещё занят — он во временной папке */ }
}
