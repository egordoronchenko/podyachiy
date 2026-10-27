initSidebar("help");
initTheme();
// раздел для администратора обычному пользователю не нужен: только длиннее читать
initUser().then(() => {
  loadSidebar();
  fillAi();
  fillFoot();
  // личный режим: входа, пользователей и прав нет — всё, что про них (класс corp), скрыто
  if (me && me.personal) document.querySelectorAll(".corp").forEach(el => { el.hidden = true; });
  if (me && me.admin && !me.personal) return;
  document.getElementById("ai-admin").hidden = true;
  if (me && me.admin) return;         // личный режим: человек один, «Администратору» — ему
  document.getElementById("admin").hidden = true;
  document.querySelector('.toc a[href="#admin"]').hidden = true;
  document.getElementById("toc-install").hidden = true;
});

// Подвал: версия сервиса и где написать автору. Версию знает проверка живости — она открыта и без входа.
async function fillFoot() {
  let version = "";
  try { version = (await api("/api/status")).version; } catch (e) { /* без версии подвал короче */ }
  document.getElementById("helpfoot").innerHTML = `Подьячий${version ? " " + esc(version) : ""} ·
    <a href="/about">О сервисе: автор, вопросы и предложения, лицензии</a>`;
}

// «Интеграция с ИИ»: адрес сервиса — тот, что сервис считает своим (PUBLIC_URL), а не из адресной строки
function fillAi() {
  const url = (me && me.url) || location.origin;
  const text = `Установи навык для работы с Подьячим: прочитай инструкцию по адресу ${url}/skill и выполни её`;
  document.getElementById("aitext").textContent = text;
  document.getElementById("aifile").textContent = `# Podyachiy\nurl=${url}\nkey=stg_…ваш ключ…\nlogin=\npassword=`;
  document.getElementById("aicursor").textContent =
    JSON.stringify({ networkPolicy: { allow: [new URL(url).hostname] } });
  const copy = document.getElementById("aicopy"), msg = document.getElementById("aimsg");
  copy.innerHTML = icon("clip", 15) + "<span>Копировать текст</span>";
  copy.onclick = async () => {
    const ok = await copyText(text);
    msg.className = "msg " + (ok ? "ok" : "err");
    msg.textContent = ok ? "Скопировано — вставьте в чат клиента." : "Не удалось скопировать — выделите текст и скопируйте вручную.";
  };
  // личный режим: входа нет — ни выбора способа, ни ключа в файле
  const personal = !!(me && me.personal);
  // Адрес сервиса в .env не задан, и в текст попал адрес этой вкладки — локальный: по нему навык
  // поставится только на этом же компьютере. В личном режиме так и задумано: другой компьютер ни при чём.
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(new URL(url).hostname);
  const warn = document.getElementById("ailocal");
  warn.hidden = personal || !me || me.url_set || !local;
  warn.innerHTML = `${icon("alert", 14)}<span><b>В тексте стоит локальный адрес — ${esc(url)}.</b> Он работает только на том
    компьютере, где запущен сервис: с другого компьютера навык по нему не установится. Адрес сервиса не задан:
    ${me && me.admin ? `впишите в <code>.env</code> строку <code>PUBLIC_URL</code> — адрес, по которому сервис открывают
      люди, например <code>http://192.0.2.10:8081</code>, — и пересоздайте контейнер (<code>docker compose up -d</code>).`
      : `попросите администратора сервиса задать его (<code>PUBLIC_URL</code>) или откройте сервис по его адресу в сети.`}</span>`;
  document.getElementById("ai-personal").hidden = !personal;
  document.getElementById("ai-how").hidden = personal;
  document.getElementById("ai-file").hidden = personal;
  document.getElementById("ai-check").hidden = personal;
  document.getElementById("ainum3").textContent = personal ? "2" : "3";
  // ключи выпускаются в окне, которое есть на каждой странице: открываем его прямо здесь
  const keys = document.getElementById("aikeys");
  keys.innerHTML = icon("key", 15) + "<span>Выпустить ключ</span>";
  keys.onclick = openKeys;
}
