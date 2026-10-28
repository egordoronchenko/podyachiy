/* Администрирование: «Настройки» — оформление. */
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
