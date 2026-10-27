initSidebar("about");
initTheme();
document.getElementById("aboutlogo").src = LOGO;
document.getElementById("copy").innerHTML = icon("clip", 15) + "<span>Скопировать сведения для обращения</span>";

function draw(a) {
  const version = a.version || "не указана";
  document.getElementById("aboutname").textContent = a.name;
  document.getElementById("abouttag").textContent = a.tagline;
  const built = [a.build.date && `собрана ${esc(a.build.date)}`, a.build.commit && `коммит ${esc(a.build.commit)}`].filter(Boolean).join(", ");
  document.getElementById("install").innerHTML = `<dl class="kv aboutkv">
      <dt>Версия</dt><dd><b>${esc(version)}</b>${built ? ` <span class="dim">· ${built}</span>` : ""}</dd>
      <dt>Лицензия</dt><dd>MIT — открытый код, бесплатно</dd>
      <dt>Режим</dt><dd>${a.mode === "personal" ? "личный — без входа, один человек" : "корпоративный — вход, пользователи, папки"}</dd>
    </dl>${me && me.admin ? `<p class="licwhy"><a href="/install-guide">Инструкция по установке</a> — настройки, сеть,
      HTTPS, почта, резервная копия и обновление.</p>` : ""}`;

  const subject = encodeURIComponent(`${a.name} ${version}: вопрос`);
  document.getElementById("author").innerHTML = `<dl class="kv aboutkv">
      <dt>Автор</dt><dd>${esc(a.author)}</dd>
      <dt>Почта</dt><dd><a href="mailto:${esc(a.email)}?subject=${subject}">${esc(a.email)}</a></dd>
      <dt>Telegram</dt><dd><a href="${esc(a.telegram_url)}" target="_blank" rel="noopener">${esc(a.telegram)}</a></dd>
    </dl>
    <p class="licwhy">Вопросы и предложения — на почту или в Telegram. В письме уже будет подставлена версия сервиса.</p>
    <div class="note">Сервис распространяется бесплатно и «как есть», без гарантий и без поддержки: автор отвечает на вопросы по возможности.</div>`;

  const facts = [`${a.name} ${version}` + (built ? ` (${[a.build.date && `сборка ${a.build.date}`, a.build.commit && `коммит ${a.build.commit}`].filter(Boolean).join(", ")})` : ""),
    `Режим: ${a.mode === "personal" ? "личный" : "корпоративный"}`,
    `Модель распознавания: ${a.speech_model}`,
    `Браузер: ${navigator.userAgent}`].join("\n");
  document.getElementById("facts").textContent = facts;
  document.getElementById("copy").onclick = async () => {
    const ok = await copyText(facts);
    say("copymsg", ok ? "ok" : "err", ok ? "Скопировано — вставьте в письмо или сообщение автору."
                                         : "Не удалось скопировать — выделите текст ниже и скопируйте вручную.");
  };

  const doc = (key, label, what) => a.documents[key]
    ? `<li><a href="/api/about/${key}" target="_blank" rel="noopener">${label}</a> — ${what}</li>`
    : `<li>${label} — в этой сборке файла нет.</li>`;
  document.getElementById("third").innerHTML = `<ul class="plain">
      ${doc("license", "Лицензия сервиса (MIT)", "на каких условиях вы им пользуетесь.")}
      ${doc("third-party", "Сторонние компоненты", "библиотеки и программы в составе сервиса, их лицензии и где взять исходные тексты.")}
    </ul>`;
}

initUser().then(async () => {
  if (!me || !me.login) return;
  loadSidebar();
  try { draw(await api("/api/about")); }
  catch (err) { document.getElementById("install").innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
});
