/* Администрирование: «Расходы» — отчёт, графики и журнал действий. */
/* ---------- расходы ----------
   Источник — журнал событий: строка на каждый прогон. Период считается по времени прогона,
   расход относится к папке записи и к человеку: кто запустил, а если сервис взял запись сам —
   кто её загрузил. Суммы — в валюте поставщика, который ответил: рубли у RouterAI, доллары
   у OpenRouter. Валюты не пересчитываются и не складываются: у каждой свой итог, своя колонка
   и свои графики, доля считается внутри валюты. */
const hours = s => (s / 3600).toFixed(1) + " ч";
const STAGES = { speech: "распознавание", triage: "разбор записи", actions: "поручения",
                 summary: "протокол", earlier: "до появления журнала" };
const PRESETS = [["today", "Сегодня", "day"], ["week", "Неделя", "day"], ["month", "Месяц", "day"],
                 ["year", "Год", "month"], ["all", "Всё время", "month"], ["custom", "Свои даты", null]];
const BUCKETS = [["day", "по дням"], ["week", "по неделям"], ["month", "по месяцам"], ["year", "по годам"]];
const GROUPS = [["folders", "Папки"], ["users", "Пользователи"], ["records", "Записи"], ["stages", "Этапы и модели"],
                ["providers", "Поставщики"]];
/* Виды графика по времени — у каждого свой вопрос. Столбики: когда тратили и на что (разбивка
   внутри периода). Линия: весь ход целиком, когда точек сотни. Нарастающим итогом: сколько
   набежало к дате. Цена часа: дорожает ли обработка и какой был объём. Календарь: в какие дни
   и недели была нагрузка. */
const VIEWS = [["bars", "Столбики"], ["line", "Линия"], ["cum", "Нарастающим итогом"],
               ["rate", "Цена часа и объём"], ["cal", "Календарь"]];
const SPLITS = [["", "без разбивки"], ["folders", "по папкам"], ["users", "по людям"], ["stages", "по этапам"]];
const MONTHS = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
const WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];
const RECORDS_SHOWN = 100;
const TOP_SERIES = 5;           // рядов в стопке; остальное — «прочее»: больше цветов глаз не различит
const TOP_ROWS = 10;            // строк в диаграмме «на что ушло»
const CAL_MIN_DAYS = 14;        // календарь короче двух недель — одна строчка клеток
const PRINT_WIDTH = 680;        // ширина графика на листе A4
let cost = { preset: "month", bucket: "day", group: "folders", start: "", end: "", cur: "", view: "bars", split: "",
             data: null, prev: null };

const costPane = document.querySelector('[data-pane="costs"]');
/* Идёт печать: графики — под ширину листа, таблицы раскрыты, разрезы — все подряд. Страница
   перестраивается по событию beforeprint, до раскладки на листы, и возвращается по afterprint. */
let printing = false;

/* Выбранный вид живёт в адресе (/admin?period=year&view=cum#costs — ссылку можно отправить)
   и в памяти браузера: адрес важнее, без него берётся то, что смотрели в прошлый раз. */
const COST_KEY = "costs-view";
const COST_ADDRESS = { preset: "period", bucket: "step", view: "view", split: "split", group: "group", cur: "cur",
                       start: "from", end: "to" };
const COST_CHOICES = { preset: PRESETS, bucket: BUCKETS, view: VIEWS, split: SPLITS, group: GROUPS };

function costRecall() {
  const q = new URLSearchParams(location.search.length > 1 ? location.search : localStorage.getItem(COST_KEY) || "");
  for (const [field, name] of Object.entries(COST_ADDRESS)) {
    const v = q.get(name);
    if (v !== null && (COST_CHOICES[field] ? COST_CHOICES[field].some(c => c[0] === v) : /^[\w-]{1,10}$/.test(v)))
      cost[field] = v;
  }
}

function costRemember() {
  if (costPane.hidden) return;                  // пока грузилось, человек ушёл в другой раздел
  const q = new URLSearchParams();
  for (const [field, name] of Object.entries(COST_ADDRESS))
    if (cost[field] && (cost.preset === "custom" || (field !== "start" && field !== "end"))) q.set(name, cost[field]);
  localStorage.setItem(COST_KEY, q.toString());
  window.history.replaceState(null, "", `${location.pathname}?${q}#costs`);
}
// в других разделах вид отчёта в адресе ни к чему
addEventListener("hashchange", () => {
  if (location.hash !== "#costs" && location.search) window.history.replaceState(null, "", location.pathname + location.hash);
});

const isoDay = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const asDate = day => new Date(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10));
function presetRange(preset) {
  const now = new Date(), end = isoDay(now);
  if (preset === "today") return [end, end];
  if (preset === "week") { const d = new Date(now); d.setDate(d.getDate() - (d.getDay() + 6) % 7); return [isoDay(d), end]; }
  if (preset === "month") return [end.slice(0, 8) + "01", end];
  if (preset === "year") return [end.slice(0, 5) + "01-01", end];
  return ["", ""];
}

/* С чем сравнивать итоги: такой же отрезок прошлого месяца или года, прошлая неделя до того же
   дня недели, вчера; у своих дат — столько же дней сразу перед ними. У «всего времени» прошлого нет. */
function previousRange() {
  if (!cost.start || !cost.end) return null;
  const months = { month: 1, year: 12 }[cost.preset];
  const days = { today: 1, week: 7 }[cost.preset] || Math.round((asDate(cost.end) - asDate(cost.start)) / 864e5) + 1;
  return [cost.start, cost.end].map(day => {
    const d = asDate(day);
    if (!months) { d.setDate(d.getDate() - days); return isoDay(d); }
    const to = new Date(d.getFullYear(), d.getMonth() - months, 1);         // 31 марта → 28 февраля, а не 3 марта
    to.setDate(Math.min(d.getDate(), new Date(to.getFullYear(), to.getMonth() + 1, 0).getDate()));
    return isoDay(to);
  });
}

function periodOf(day, bucket) {
  if (bucket === "year") return day.slice(0, 4);
  if (bucket === "month") return day.slice(0, 7);
  const d = asDate(day);
  if (bucket === "week") d.setDate(d.getDate() - (d.getDay() + 6) % 7);
  return isoDay(d);
}
// подпись на оси — короткая; в подсказке и таблице — полная, с годом
const periodShort = (p, bucket, first) =>
  bucket === "year" ? p
  : bucket === "month" ? MONTHS[+p.slice(5, 7) - 1] + (first || p.slice(5, 7) === "01" ? " " + p.slice(0, 4) : "")
  : `${p.slice(8, 10)}.${p.slice(5, 7)}`;
const periodName = (p, bucket) =>
  bucket === "year" ? p + " год"
  : bucket === "month" ? `${MONTHS[+p.slice(5, 7) - 1]} ${p.slice(0, 4)}`
  : bucket === "week" ? `неделя с ${ruDay(p)}` : ruDay(p);

/* Ряд по времени без пропусков: день, когда ничего не тратили, — тоже точка на оси,
   иначе неделя тишины выглядит как соседние дни. */
function timeline() {
  const d = cost.data;
  if (!d.series.length) return [];
  const known = new Map(d.series.map(s => [s.period, s]));
  const last = d.end ? periodOf(d.end, d.bucket) : d.series.at(-1).period;
  const first = d.start ? periodOf(d.start, d.bucket) : d.series[0].period;
  const at = new Date(+first.slice(0, 4), (+first.slice(5, 7) || 1) - 1, +first.slice(8, 10) || 1);
  const key = () => periodOf(isoDay(at), d.bucket);
  const out = [];
  for (; key() <= last; d.bucket === "year" ? at.setFullYear(at.getFullYear() + 1)
                        : d.bucket === "month" ? at.setMonth(at.getMonth() + 1)
                        : at.setDate(at.getDate() + (d.bucket === "week" ? 7 : 1)))
    out.push(known.get(key()) || { period: key(), cost: {}, runs: 0, records: 0, audio_sec: 0 });
  return out;
}

async function loadCosts() {
  if (cost.preset !== "custom") [cost.start, cost.end] = presetRange(cost.preset);
  if (cost.view === "cal") cost.bucket = "day";             // календарь состоит из дней
  const before = previousRange();
  const ask = ([start, end]) => api(`/api/admin/report?start=${start}&end=${end}&bucket=${cost.bucket}`);
  try {
    [cost.data, cost.prev] = await Promise.all([ask([cost.start, cost.end]), before && ask(before)]);
    say("costmsg", "", "");
  } catch (err) { say("costmsg", "err", err.message); return; }
  renderCosts();
  loadActions();
}

/* ---------- графики ----------
   Без библиотек: SVG собирается строкой. У каждого графика одна остановка для Tab, дальше по
   точкам ходят стрелками; подсказка одна и та же по наведению и по фокусу, те же числа — в таблице
   под графиком. Цвет нигде не единственный носитель: рядом легенда с суммами и подписи. */
const axisNumber = new Intl.NumberFormat("ru", { maximumFractionDigits: 3 });
const px = v => Math.round(v * 10) / 10;

// «круглая» шкала от нуля: три–пять делений
function axisScale(max) {
  if (!(max > 0)) return { top: 1, ticks: [0] };
  const raw = max / 4, pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].find(m => m * pow >= raw) * pow;
  const count = Math.ceil(max / step - 1e-9);
  return { top: count * step, ticks: Array.from({ length: count + 1 }, (_, i) => i * step) };
}

// строки подсказки: [название, значение, цвет ряда]; say — то же самое словами для экранного диктора
const tipHtml = (title, rows) => `<b class="h">${esc(title)}</b>` + rows.map(([name, value, color]) =>
  `<div class="r">${color ? `<i style="background:${color}"></i>` : ""}<span>${esc(name)}</span><b>${esc(value)}</b></div>`).join("");
const tipSay = (title, rows) => `${title}: ${rows.map(([name, value]) => `${name} ${value}`).join(", ")}`;
const mark = (i, n, title, rows, inner) =>
  `<g class="pt" role="img" tabindex="${i === n - 1 ? 0 : -1}" aria-label="${esc(tipSay(title, rows))}" data-tip="${esc(tipHtml(title, rows))}">${inner}</g>`;

/* График по времени. o.kind: "bars" — столбики (o.colors и p.parts — стопкой), "line" — линия.
   Точка: {total, parts, short, title, rows, label}; total === null — в этом периоде значения нет.
   Столбики не уже 8 px: когда не помещаются, блок прокручивается вбок, а шкала слева стоит. */
function drawSeries(o, width) {
  const n = o.points.length, H = o.height || 220, top = 20, bottom = 22, axisW = 50;
  const room = Math.max(140, width - axisW);
  const bars = o.kind === "bars";
  const slot = bars && !printing ? Math.max(8, room / n) : room / n;
  const W = slot * n > room + .5 ? Math.ceil(slot * n) : room;
  const sc = axisScale(Math.max(0, ...o.points.map(p => p.total || 0), o.ref ? o.ref.value : 0));
  const plot = H - top - bottom, y = v => px(top + plot * (1 - v / sc.top)), y0 = y(0);
  const cx = i => px(slot * (i + .5));
  const colors = o.colors || ["var(--viz-1)"];

  const every = Math.max(1, Math.ceil(56 / slot)), centered = slot >= 56;
  const xlabels = o.points.map((p, i) => i % every || (!centered && i * slot + p.short.length * 7 > W) ? "" :
    `<text x="${centered ? cx(i) : px(i * slot + 1)}" y="${H - 6}"${centered ? ' text-anchor="middle"' : ""}>${esc(p.short)}</text>`).join("");

  let marks = "", under = "";
  if (bars) {
    const bw = Math.max(1, Math.min(28, slot - 2));
    const labelled = n <= 16 && slot >= 50;
    marks = o.points.map((p, i) => {
      const x = px(i * slot + (slot - bw) / 2), parts = (p.parts || [p.total]).map((v, k) => [v, colors[k]]).filter(([v]) => v > 0);
      const full = p.total > 0 ? Math.max(2, p.total / sc.top * plot) : 0;      // расход есть — столбик виден, каким бы малым он ни был
      let floor = y0, body = "";
      parts.forEach(([v, color], k) => {
        const h = v / p.total * full, last = k === parts.length - 1;
        const gap = k && h > 4 ? 2 : 0;                             // ряды в стопке разделяет просвет, а не обводка
        const up = px(floor - h), r = last ? Math.min(4, bw / 2, h) : 0;
        body += `<path style="fill:${color}" d="M${x},${px(floor - gap)}V${px(up + r)}Q${x},${up} ${px(x + r)},${up}H${px(x + bw - r)}Q${px(x + bw)},${up} ${px(x + bw)},${px(up + r)}V${px(floor - gap)}Z"/>`;
        floor -= h;
      });
      return mark(i, n, p.title, p.rows,
        `<rect class="band" x="${px(i * slot)}" y="1" width="${px(slot)}" height="${H - bottom - 1}"/>${body}` +
        (labelled && p.total > 0 ? `<text class="val" x="${cx(i)}" y="${px(y0 - full - 5)}" text-anchor="middle">${esc(p.label)}</text>` : ""));
    }).join("");
  } else {
    // линия рвётся там, где значения нет: соединять через пропуск — значит выдумывать ход
    const runs = [];
    o.points.forEach((p, i) => {
      if (p.total === null) return;
      if (i && o.points[i - 1].total !== null) runs.at(-1).push(i); else runs.push([i]);
    });
    const shown = o.points.filter(p => p.total !== null).length;
    under = runs.map(run => {
      const path = run.map((i, k) => `${k ? "L" : "M"}${cx(i)},${y(o.points[i].total)}`).join("");
      return (run.length > 1 ? `<path class="area" d="${path}V${y0}H${cx(run[0])}Z"/>` : "") + `<path class="line" d="${path}"/>`;
    }).join("");
    marks = o.points.map((p, i) => {
      const has = p.total !== null, lone = has && (o.points[i - 1] || {}).total == null && (o.points[i + 1] || {}).total == null;
      const left = cx(i) < W / 2;
      return mark(i, n, p.title, p.rows,
        `<rect class="band" x="${px(i * slot)}" y="1" width="${px(Math.max(slot, 1))}" height="${H - bottom - 1}"/>
         <line class="cross" x1="${cx(i)}" x2="${cx(i)}" y1="${top}" y2="${y0}"/>` +
        (has ? `<circle class="dot${shown <= 40 || lone ? "" : " far"}" cx="${cx(i)}" cy="${y(p.total)}" r="4"/>` : "") +
        (has && o.marks.includes(i) ? `<text class="val" x="${px(cx(i) + (left ? 8 : -8))}" y="${px(Math.max(12, y(p.total) - 8))}"${
          left ? "" : ' text-anchor="end"'}>${esc(p.label)}</text>` : ""));
    }).join("");
  }
  const grid = sc.ticks.map(t => `<line class="${t ? "grid" : "base"}" x1="0" x2="${W}" y1="${y(t)}" y2="${y(t)}"/>`).join("");
  const ref = o.ref ? `<line class="ref" x1="0" x2="${W}" y1="${y(o.ref.value)}" y2="${y(o.ref.value)}"/>
    <text class="val" x="4" y="${px(y(o.ref.value) - 5)}">${esc(o.ref.label)}</text>` : "";
  return `<div class="viz ${bars ? "vbars" : "vlines"}" role="group" aria-label="${esc(o.label)}. По точкам — стрелками влево и вправо; те же числа — в таблице ниже">
    <svg class="axis" width="${axisW}" height="${H}" aria-hidden="true">${sc.ticks.map(t =>
      `<text x="${axisW - 7}" y="${y(t) + 4}" text-anchor="end">${axisNumber.format(t)}</text>`).join("")}</svg>
    <div class="vizscroll"><svg width="${W}" height="${H}">${grid}${under}${ref}${marks}${xlabels}</svg></div>
    <div class="viztip" hidden aria-hidden="true"></div></div>`;
}

/* Календарь: неделя — столбец, день — клетка, чем больше потрачено, тем плотнее цвет (один тон,
   по светлоте — читается и без различения цветов). Уровни — по четвертям дней с расходом, а не
   долями от максимума: один дорогой день не делает все остальные одинаково бледными. */
function drawCalendar(days, cur, width) {
  const paid = days.map(s => s.cost[cur] || 0).filter(Boolean).sort((a, b) => a - b);
  const edges = [...new Set([.25, .5, .75, 1].map(q => paid[Math.max(0, Math.ceil(paid.length * q) - 1)]))];   // верхние границы уровней
  const level = v => v ? 5 - edges.length + edges.findIndex(e => v <= e) : 0;
  const lead = (asDate(days[0].period).getDay() + 6) % 7, weeks = Math.ceil((days.length + lead) / 7);
  const axisW = 28, top = 20;
  const pitch = printing ? Math.min(30, Math.floor((width - axisW) / weeks))
                                   : Math.max(14, Math.min(34, Math.floor((width - axisW) / weeks)));
  const cell = pitch - 3, numbered = pitch >= 26, H = top + 7 * pitch;
  const months = days.map((s, i) => ({ i, col: Math.floor((i + lead) / 7), day: s.period }))
    .filter(m => !m.i || m.day.endsWith("-01"));
  if (months.length > 1 && (months[1].col - months[0].col) * pitch < 58) months.shift();    // обрывок месяца в начале: подписи слиплись бы
  const cells = days.map((s, i) => {
    const v = s.cost[cur] || 0, lv = level(v), x = Math.floor((i + lead) / 7) * pitch, y0 = top + (i + lead) % 7 * pitch;
    const rows = [[`потрачено, ${sign(cur)}`, v ? amount(v, cur) : "—"], ["записей", s.records], ["звука", hours(s.audio_sec)]];
    return mark(i, days.length, `${WEEKDAYS[(i + lead) % 7]}, ${ruDay(s.period)}`, rows,
      `<rect class="cell l${lv}" x="${x + 1}" y="${y0 + 1}" width="${cell}" height="${cell}" rx="3"/>` +
      (numbered ? `<text class="day${lv > 2 ? " on" : ""}" x="${x + 1 + cell / 2}" y="${y0 + cell / 2 + 5}" text-anchor="middle">${+s.period.slice(8)}</text>` : ""));
  }).join("");
  return `<div class="viz cal" data-jump="7" role="group" aria-label="Календарь расходов в ${sign(cur)} по дням. Стрелки вверх и вниз — день, влево и вправо — неделя; те же числа — в таблице ниже">
    <svg class="axis" width="${axisW}" height="${H}" aria-hidden="true">${WEEKDAYS.map((w, r) =>
      pitch >= 22 || r % 2 === 0 ? `<text x="0" y="${top + r * pitch + cell / 2 + 5}">${w}</text>` : "").join("")}</svg>
    <div class="vizscroll"><svg width="${weeks * pitch}" height="${H}">${months.map((m, k) =>
      `<text x="${m.col * pitch + 1}" y="12">${MONTHS[+m.day.slice(5, 7) - 1]}${!k || m.day.slice(5, 7) === "01" ? " " + m.day.slice(0, 4) : ""}</text>`).join("")}${cells}</svg></div>
    <div class="viztip" hidden aria-hidden="true"></div></div>
    <ul class="legend" aria-label="Шкала календаря">
      <li><i class="l0"></i>нет расходов</li>${edges.map((e, k) =>
        `<li><i style="background:var(--heat-${5 - edges.length + k})"></i>${k ? "до" : "день до"} ${amount(e, cur)}</li>`).join("")}</ul>`;
}

function showTip(pt) {
  const viz = pt.closest(".viz"), tip = viz.querySelector(".viztip");
  tip.innerHTML = pt.dataset.tip;
  tip.hidden = false;
  const at = pt.getBoundingClientRect(), box = viz.getBoundingClientRect();
  let left = at.right - box.left + 8;
  if (left + tip.offsetWidth > box.width) left = at.left - box.left - 8 - tip.offsetWidth;
  tip.style.left = Math.max(0, Math.min(left, box.width - tip.offsetWidth)) + "px";
  // у календаря подсказка над клеткой или под ней — саму клетку не закрывает
  const above = at.top - box.top - tip.offsetHeight - 6;
  tip.style.top = (viz.classList.contains("cal") ? (above < -40 ? at.bottom - box.top + 6 : above) : 0) + "px";
}
const hideTips = () => costPane.querySelectorAll(".viztip").forEach(tip => { tip.hidden = true; });

costPane.addEventListener("pointerover", e => { const pt = e.target.closest(".pt"); if (pt) showTip(pt); });
costPane.addEventListener("pointerout", e => {
  // палец «уходит» сразу после касания — подсказка остаётся до следующего касания
  if (e.pointerType !== "touch" && e.target.closest(".viz") && !(e.relatedTarget && e.relatedTarget.closest(".viz"))) hideTips();
});
costPane.addEventListener("focusin", e => { const pt = e.target.closest(".pt"); if (pt) showTip(pt); });
costPane.addEventListener("focusout", e => { if (e.target.closest(".pt")) hideTips(); });
costPane.addEventListener("keydown", e => {
  const pt = e.target.closest(".pt");
  if (!pt) return;
  if (e.key === "Escape") { hideTips(); return; }
  const all = [...pt.closest(".viz").querySelectorAll(".pt")], at = all.indexOf(pt);
  const jump = +pt.closest(".viz").dataset.jump || 0;       // календарь: влево-вправо — на неделю
  const to = { ArrowLeft: at - (jump || 1), ArrowRight: at + (jump || 1), ArrowUp: at - 1, ArrowDown: at + 1,
               Home: 0, End: all.length - 1 }[e.key];
  if (to === undefined || (!jump && (e.key === "ArrowUp" || e.key === "ArrowDown"))) return;
  e.preventDefault();
  const next = all[Math.max(0, Math.min(all.length - 1, to))];
  pt.setAttribute("tabindex", "-1");
  next.setAttribute("tabindex", "0");
  next.focus();
});

const legend = series => `<ul class="legend">${series.map(s =>
  `<li><i style="background:${s.color}"></i>${esc(s.name)} <b>${esc(s.value)}</b></li>`).join("")}</ul>`;
const sheet = (head, rows) => `<details class="plain"${printing ? " open" : ""}><summary>Таблицей</summary>
  <div class="scroll"><table><thead><tr>${head.map((h, i) => `<th${i ? ' class="num"' : ""}>${h}</th>`).join("")}</tr></thead><tbody>${
    rows.map(row => `<tr>${row.map((c, i) => `<td${i ? ' class="num r"' : ""}>${c}</td>`).join("")}</tr>`).join("")
  }</tbody></table></div></details>`;
const dash = `<span class="dim">—</span>`;

/* Столбик по разрезу: пять самых дорогих за весь период и «прочее». У этапов порядок и цвет
   постоянные — распознавание всегда первое и синее, какой бы период ни смотрели. */
function splitSeries(line, kind, cur) {
  const d = cost.data, cells = d.split[kind], sums = new Map(), stages = Object.keys(STAGES);
  for (const s of line)
    for (const [key, sum] of Object.entries(cells[s.period] || {}))
      if (sum[cur]) sums.set(key, (sums.get(key) || 0) + sum[cur]);
  const order = kind === "stages" ? stages.filter(k => sums.has(k))
    : [...sums.keys()].sort((a, b) => sums.get(b) - sums.get(a)).slice(0, TOP_SERIES);
  const name = key => kind === "stages" ? STAGES[key]
    : kind === "users" ? (key ? d.names[key] || key : "неизвестно") : key || "вне папок";
  const series = order.map((key, i) => ({ name: name(key), total: sums.get(key),
                                          color: `var(--viz-${(kind === "stages" ? stages.indexOf(key) : i) + 1})` }));
  const others = [...sums.keys()].filter(k => !order.includes(k));
  if (others.length) series.push({ name: "прочее", color: "var(--viz-rest)", total: others.reduce((a, k) => a + sums.get(k), 0) });
  const rows = line.map(s => {
    const cell = cells[s.period] || {}, of = k => (cell[k] || {})[cur] || 0;
    return [...order.map(of), ...(others.length ? [others.reduce((a, k) => a + of(k), 0)] : [])];
  });
  return { series, rows };
}

function renderSeries() {
  const d = cost.data, cur = cost.cur, curs = d.currencies, line = timeline();
  const box = document.getElementById("costseries"), bar = document.getElementById("costview");
  const of = (sum, c) => sum[c] || 0, paid = v => v ? amount(v, cur) : dash;
  const step = BUCKETS.find(b => b[0] === d.bucket)[1], unit = sign(cur);
  const width = printing ? PRINT_WIDTH : box.clientWidth;
  const days = d.start && d.end ? Math.round((asDate(d.end) - asDate(d.start)) / 864e5) + 1
    : line.length * { day: 1, week: 7, month: 30, year: 365 }[d.bucket];
  const calendar = days >= CAL_MIN_DAYS;
  const view = cost.view === "cal" && !calendar ? "bars" : cost.view;
  bar.hidden = !line.length || !cur;
  if (bar.hidden) { box.innerHTML = `<div class="empty">${icon("idle", 17)}за этот период расходов не было</div>`; return; }
  bar.innerHTML = `
    <div><label id="viewlabel">Вид</label>
      <div class="tabs" role="group" aria-labelledby="viewlabel">${VIEWS.filter(v => v[0] !== "cal" || calendar).map(([id, label]) =>
        `<button type="button" data-view="${id}" aria-pressed="${view === id}" aria-selected="${view === id}">${label}</button>`).join("")}</div></div>
    ${view === "bars" ? `<div><label for="costsplit">Внутри столбика</label><select id="costsplit">${SPLITS.map(([id, label]) =>
      `<option value="${id}"${cost.split === id ? " selected" : ""}>${label}</option>`).join("")}</select></div>` : ""}`;

  const point = (s, i, total, rows) => ({ total, rows, label: total ? amount(total, cur) : "", title: periodName(s.period, d.bucket),
                                          short: periodShort(s.period, d.bucket, !i) });
  const volume = s => [["записей", s.records], ["звука", hours(s.audio_sec)], ["прогонов", s.runs]];
  const spent = s => [...curs.map(c => [`потрачено, ${sign(c)}`, of(s.cost, c) ? amount(of(s.cost, c), c) : "—"]), ...volume(s)];
  const plain = () => sheet(["период", ...curs.map(c => `потрачено, ${sign(c)}`), "записей", "звука", "прогонов"],
    line.map(s => [periodName(s.period, d.bucket), ...curs.map(c => of(s.cost, c) ? amount(of(s.cost, c), c) : dash),
                   s.records, hours(s.audio_sec), s.runs]));
  const title = text => `<h3 class="viztitle">${text}</h3>`;
  let html;

  if (view === "bars" && cost.split) {
    const { series, rows } = splitSeries(line, cost.split, cur), sum = row => row.reduce((a, v) => a + v, 0);
    const what = SPLITS.find(s => s[0] === cost.split)[1];
    html = title(`Расходы ${step} ${what}, ${unit}`) +
      legend(series.map(s => ({ ...s, value: amount(s.total, cur) }))) +
      drawSeries({ kind: "bars", label: `Расходы в ${unit} ${step} ${what}`, colors: series.map(s => s.color),
        points: line.map((s, i) => ({ ...point(s, i, sum(rows[i]), [
          ...series.map((ser, k) => [ser.name, rows[i][k] ? amount(rows[i][k], cur) : "—", ser.color]).filter(r => r[1] !== "—"),
          [`всего, ${unit}`, sum(rows[i]) ? amount(sum(rows[i]), cur) : "—"], ...volume(s)]), parts: rows[i] })) }, width) +
      (series.at(-1).name === "прочее" ? `<div class="note" style="margin-top:12px">Показаны ${TOP_SERIES} самых дорогих за период, остальные сложены в «прочее». Полный список — в таблице разреза ниже.</div>` : "") +
      sheet(["период", ...series.map(s => esc(s.name)), `всего, ${unit}`],
        line.map((s, i) => [periodName(s.period, d.bucket), ...rows[i].map(paid), paid(sum(rows[i]))]));
  } else if (view === "bars" || view === "line") {
    const points = line.map((s, i) => point(s, i, of(s.cost, cur), spent(s)));
    const peak = points.reduce((best, p, i) => p.total > points[best].total ? i : best, 0);
    html = title(`Расходы ${step}, ${unit}`) +
      drawSeries({ kind: view, label: `Расходы в ${unit} ${step}`, points, marks: [peak] }, width) + plain();
  } else if (view === "cum") {
    let run = 0;
    const totals = line.map(s => run += of(s.cost, cur));
    html = title(`Расходы нарастающим итогом, ${unit}`) +
      drawSeries({ kind: "line", label: `Расходы в ${unit} нарастающим итогом ${step}`, marks: [line.length - 1],
        points: line.map((s, i) => point(s, i, totals[i], [[`с начала периода, ${unit}`, amount(totals[i], cur)],
                                                             [`за период, ${unit}`, of(s.cost, cur) ? amount(of(s.cost, cur), cur) : "—"]])) }, width) +
      sheet(["период", `за период, ${unit}`, `нарастающим итогом, ${unit}`],
        line.map((s, i) => [periodName(s.period, d.bucket), paid(of(s.cost, cur)), amount(totals[i], cur)]));
  } else if (view === "rate") {
    // цена часа — внутри валюты: расход в ней на звук записей, за которые в ней платили
    const heard = s => of(d.paid_audio.series[s.period] || {}, cur), rate = s => heard(s) ? of(s.cost, cur) / (heard(s) / 3600) : null;
    const all = of(d.paid_audio.total, cur), mean = all ? of(d.total.cost, cur) / (all / 3600) : 0;
    const small = Math.max(220, printing || width < 620 ? width : (width - 24) / 2);
    const bars = (label, value, show) => drawSeries({ kind: "bars", label: `${label} ${step}`, height: 150,
      points: line.map((s, i) => ({ ...point(s, i, value(s), volume(s)), label: value(s) ? show(value(s)) : "" })) }, small);
    html = title(`Цена часа звука ${step}, ${unit}`) +
      drawSeries({ kind: "line", label: `Цена часа звука в ${unit} ${step}`, marks: [],
        ref: mean ? { value: mean, label: `в среднем за период ${amount(mean, cur)}` } : null,
        points: line.map((s, i) => point(s, i, rate(s), [[`за час звука, ${unit}`, rate(s) === null ? "—" : amount(rate(s), cur)],
          [`потрачено, ${unit}`, of(s.cost, cur) ? amount(of(s.cost, cur), cur) : "—"], ["оплачено звука", hours(heard(s))]])) }, width) +
      `<div class="vizpair"><div>${title("Часов звука")}${bars("Часов звука", s => s.audio_sec / 3600, v => v.toFixed(1).replace(".", ","))}</div>
        <div>${title("Записей")}${bars("Записей", s => s.records, String)}</div></div>
      <div class="note" style="margin-top:12px">Цена часа — расход в ${unit}, делённый на часы звука тех записей, за которые платили в ${unit}.
        Пересборка протокола прибавляет деньги, но не часы: период, где записи только пересобирали, выглядит дороже.</div>` +
      sheet(["период", `за час звука, ${unit}`, `потрачено, ${unit}`, `оплачено звука`, "всего звука", "записей"],
        line.map(s => [periodName(s.period, d.bucket), rate(s) === null ? dash : amount(rate(s), cur), paid(of(s.cost, cur)),
                       hours(heard(s)), hours(s.audio_sec), s.records]));
  } else {
    html = title(`Расходы по дням, ${unit}`) + drawCalendar(line, cur, width) + plain();
  }
  box.innerHTML = html;
  box.dataset.width = width;
  box.querySelectorAll(".vizscroll").forEach(el => { el.scrollLeft = el.scrollWidth; });      // свежее — справа, с него и начинаем
}

/* «На что ушло» — те же строки, что в таблице разреза, но по убыванию и в одной валюте:
   первые десять столбиками, остаток одной строкой без столбика (сумма сотен записей была бы
   длиннее любого из них и сжала бы остальные). У этапов сверху — полоса долей. */
function costChart(group) {
  const d = cost.data, cur = cost.cur, total = d.total.cost[cur] || 0;
  if (!total) return "";
  const of = sum => sum[cur] || 0, unit = sign(cur);
  const share = v => `${v / total < .01 ? "<1" : Math.round(v / total * 100)} %`;
  const [what, rows] = {
    folders: () => ["Папки: потрачено на записи самой папки, без подпапок",
                    [...d.folders.map(f => [f.folder, of(f.own)]),
                     ["вне папок", Math.round((total - d.folders.reduce((a, f) => a + of(f.own), 0)) * 1e6) / 1e6]]],
    users: () => ["Кто потратил", d.users.map(u => [u.login ? d.names[u.login] || u.login : "неизвестно", of(u.cost)])],
    records: () => ["Самые дорогие записи", d.records.map(r => [r.file.split("/").pop(), of(r.cost), r.file])],
    stages: () => ["Этапы и модели", d.stages.map(s => [(STAGES[s.stage] || s.stage) + (s.model ? ` · ${s.model}` : ""), of(s.cost)])],
    providers: () => ["Кому заплачено", d.providers.map(p => [p.provider || "не назван", of(p.cost)])],
  }[group]();
  const sorted = rows.filter(r => r[1] > 0).sort((a, b) => b[1] - a[1]), shown = sorted.slice(0, TOP_ROWS);
  const tail = sorted.slice(TOP_ROWS), top = shown.length ? shown[0][1] : 0;
  const row = ([name, v, full], cls) => `<li${cls ? ` class="${cls}"` : ""}><span class="hname" title="${esc(full || name)}">${esc(name)}</span>
    <span class="htrack">${cls ? "" : `<i style="width:${px(v / top * 100)}%"></i>`}</span>
    <span class="hval">${amount(v, cur)} <span class="dim">${share(v)}</span></span></li>`;

  let strip = "";
  if (group === "stages") {
    const sums = new Map();
    d.stages.forEach(s => of(s.cost) && sums.set(s.stage, (sums.get(s.stage) || 0) + of(s.cost)));
    const parts = Object.keys(STAGES).filter(k => sums.has(k)).map(k =>
      ({ name: STAGES[k], v: sums.get(k), color: `var(--viz-${Object.keys(STAGES).indexOf(k) + 1})` }));
    strip = `<h3 class="viztitle">Доли этапов, ${unit}</h3>
      <div class="strip" role="img" aria-label="Доли этапов в расходах в ${unit}: ${esc(parts.map(p => `${p.name} ${share(p.v)}`).join(", "))}">${
        parts.map(p => `<i style="flex-grow:${p.v};background:${p.color}" title="${esc(p.name)}: ${amount(p.v, cur)}, ${share(p.v)}"></i>`).join("")}</div>` +
      legend(parts.map(p => ({ ...p, value: `${amount(p.v, cur)} · ${share(p.v)}` })));
  }
  return strip + `<h3 class="viztitle">${what}, ${unit}</h3>
    <ol class="hbars" aria-label="${esc(what)}, ${unit}: от дорогих к дешёвым">${shown.map(r => row(r)).join("")}${
      tail.length ? row([`ещё ${tail.length}`, tail.reduce((a, r) => a + r[1], 0)], "rest") : ""}</ol>`;
}

function renderCosts() {
  const d = cost.data, t = d.total, p = cost.prev;
  // страница перерисовывается целиком: нажатая с клавиатуры кнопка не должна терять фокус
  const held = document.activeElement, again = costPane.contains(held) && (held.id ? "#" + held.id
    : ["preset", "cur", "view", "group"].map(k => held.dataset[k] && `[data-${k}="${held.dataset[k]}"]`).find(Boolean));
  const curs = d.currencies;                    // валюты, в которых за период что-то потрачено
  if (!curs.includes(cost.cur)) cost.cur = curs[0] || "";
  const curName = cur => `${cur === "RUB" ? "Рубли" : cur === "USD" ? "Доллары" : esc(cur)}, ${sign(cur)}`;
  // файл — весь отчёт по тому, что выбрано на странице: период и шаг
  const file = new URLSearchParams({ start: cost.start, end: cost.end, bucket: cost.bucket });
  // Рубли и доллары на одной оси сравнивать нельзя: графики показывают одну валюту, вторая — переключателем.
  document.getElementById("costbar").innerHTML = `
    <div><label id="presetlabel">Период</label>
      <div class="tabs" role="group" aria-labelledby="presetlabel">${PRESETS.map(([id, label]) =>
        `<button type="button" data-preset="${id}" aria-pressed="${cost.preset === id}"
                 aria-selected="${cost.preset === id}">${label}</button>`).join("")}</div></div>
    <div><label for="coststart">С</label><input type="date" id="coststart" value="${cost.start}"></div>
    <div><label for="costend">По</label><input type="date" id="costend" value="${cost.end}"></div>
    <div><label for="costbucket">Шаг графика</label><select id="costbucket">${BUCKETS.map(([id, label]) =>
      `<option value="${id}"${cost.bucket === id ? " selected" : ""}>${label}</option>`).join("")}</select></div>
    ${curs.length > 1 ? `<div><label id="curlabel">Валюта графиков</label>
      <div class="tabs" id="costcur" role="group" aria-labelledby="curlabel">${curs.map(cur =>
        `<button type="button" data-cur="${cur}" aria-pressed="${cost.cur === cur}" aria-selected="${cost.cur === cur}">${curName(cur)}</button>`).join("")}</div></div>` : ""}
    <span class="grow"></span>
    <div><label id="filelabel">Скачать отчёт целиком</label>
      <div class="files" role="group" aria-labelledby="filelabel">
        <a class="navlink" id="costxlsx" href="/api/admin/report.xlsx?${file}" download
           title="Книга Excel: сводка и по листу на каждый разрез — за выбранный период">${icon("copy", 15)}Excel</a>
        <a class="navlink" id="costcsv" href="/api/admin/report.csv?${file}" download
           title="Один файл CSV: те же таблицы подряд — за выбранный период">${icon("copy", 15)}CSV</a>
        <button type="button" class="ghost" id="costprint"
           title="Печать всего отчёта: все разрезы подряд; в окне печати выберите «Сохранить как PDF»">${icon("doc", 15)}PDF (печать)</button>
      </div></div>`;

  const now = new Date();
  document.getElementById("costprinted").textContent =
    `Подьячий, расходы. Период: ${cost.start ? ruDay(cost.start) : "с начала"} — ${cost.end ? ruDay(cost.end) : "по сегодня"}, ` +
    `шаг ${BUCKETS.find(b => b[0] === cost.bucket)[1]}. ` +
    (cost.cur ? `Графики — в ${sign(cost.cur)}${curs.length > 1 ? "; рубли и доллары не пересчитываются и не складываются" : ""}. ` : "") +
    `Отчёт сформирован ${ruDay(isoDay(now))} в ${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}` +
    `${me && me.login ? `, ${me.name || me.login}` : ""}.`;

  /* Плитка сравнивает с прошлым таким же отрезком: стрелка и процент словами, без «хорошо/плохо» —
     рост расходов сам по себе ни то ни другое. Не с чем сравнить — так и сказано. */
  const delta = (value, was) => {
    if (!p) return "";
    if (!was) return `<div class="delta">было: нет данных</div>`;
    const part = (value - was) / was;
    const text = Math.abs(part) < .005 ? "без изменений"
      : part >= 9 ? `в ${Math.round(value / was)} ${plural(Math.round(value / was), "раз", "раза", "раз")} больше`
      : `${part > 0 ? "+" : "−"}${Math.round(Math.abs(part) * 100)} %`;
    return `<div class="delta"><span aria-hidden="true">${Math.abs(part) < .005 ? "=" : part > 0 ? "▲" : "▼"}</span> ${text}</div>`;
  };
  const perHour = (r, cur) => r.paid_audio.total[cur] ? r.total.cost[cur] / (r.paid_audio.total[cur] / 3600) : 0;
  document.getElementById("costtiles").innerHTML = [
    ...(curs.length ? curs.map(cur => [`потрачено, ${sign(cur)}`, amount(t.cost[cur], cur), "coin", t.cost[cur], p && p.total.cost[cur]])
                    : [["потрачено", "—", "coin"]]),
    ["записей", t.records, "list", t.records, p && p.total.records],
    ["звука", hours(t.audio_sec), "mic", t.audio_sec, p && p.total.audio_sec],
    ["прогонов", t.runs, "clock", t.runs, p && p.total.runs],
    // цена часа — внутри валюты: расход в ней на звук записей, за которые в ней платили
    ...curs.filter(cur => perHour(d, cur)).map(cur =>
      [`за час звука, ${sign(cur)}`, amount(perHour(d, cur), cur), "coin", perHour(d, cur), p && perHour(p, cur)]),
  ].map(([l, n, ic, value, was]) => `<div class="tile"><div class="top">${icon(ic, 14)}<span class="l">${l}</span></div>
      <div class="n">${esc(n)}</div>${value ? delta(value, was) : ""}</div>`).join("");
  const earlier = p && (p.start === p.end ? ruDay(p.start) : `${ruDay(p.start)} — ${ruDay(p.end)}`);
  document.getElementById("costcompare").textContent = !p ? ""
    : p.total.runs ? `Под числом — изменение к такому же отрезку раньше: ${earlier}.`
    : `За такой же отрезок раньше (${earlier}) в журнале расходов нет — сравнивать не с чем.`;

  renderSeries();
  document.getElementById("costgroup").innerHTML = GROUPS.map(([id, label]) =>
    `<button type="button" role="tab" data-group="${id}" aria-selected="${cost.group === id}">${label}</button>`).join("");
  // на странице — один разрез, выбранный вкладкой; на печати — все подряд, у каждого свой заголовок
  document.getElementById("costchart").innerHTML = printing ? "" : costChart(cost.group);
  document.getElementById("costtable").innerHTML = !printing || !t.runs ? costTable(cost.group)
    : GROUPS.map(([id, label]) => `<section class="printgroup"><h3>${label}</h3>${costChart(id)}${costTable(id)}</section>`).join("");
  if (again && costPane.querySelector(again)) costPane.querySelector(again).focus();
  costRemember();
}

function costTable(group) {
  const d = cost.data, total = d.total.cost, curs = d.currencies;
  const empty = `<div class="empty">${icon("idle", 17)}за этот период расходов не было</div>`;
  if (!d.total.runs) return empty;
  // Колонка на валюту: сумма и под ней полоска — доля строки в итоге этой же валюты.
  // sub — потрачено на записи самой папки, без подпапок.
  const cells = (sum, sub) => curs.map(cur => {
    const v = sum[cur] || 0, part = total[cur] ? Math.round(v / total[cur] * 100) : 0;
    if (!v) return `<td class="num r curcell"><span class="dim">—</span></td>`;
    return `<td class="num r curcell"><b>${amount(v, cur)}</b>
      <div class="bar" role="img" aria-label="${part}% от итога в ${sign(cur)}" title="${part}% от итога в ${sign(cur)}"><i style="width:${v / total[cur] * 100}%"></i></div>
      ${sub && (sub[cur] || 0) !== v ? `<div class="dim sub">без подпапок ${sub[cur] ? amount(sub[cur], cur) : "—"}</div>` : ""}</td>`;
  }).join("");
  const spentHead = curs.map(cur => `<th class="num">потрачено, ${sign(cur)}</th>`).join("");
  const nums = r => `<td class="num r">${r.records}</td><td class="num r">${hours(r.audio_sec)}</td><td class="num r">${r.runs}</td>`;
  const head = first => `<div class="scroll" style="margin-top:12px"><table><thead><tr><th>${first}</th>${spentHead}
    <th class="num">записей</th><th class="num">звука</th><th class="num">прогонов</th></tr></thead><tbody>`;
  const two = curs.length > 1 ? ` Рубли и доллары не пересчитываются и не складываются: полоска под суммой — доля в итоге своей валюты.` : "";
  const note = text => `<div class="note" style="margin-top:12px">${text}${two}</div>`;

  if (group === "folders") return head("папка") + d.folders.map(f => {
    const depth = f.folder.split("/").length - 1;
    return `<tr><td style="padding-left:${depth * 18}px"><span class="fcell">${icon("folder", 14)}${esc(f.folder.split("/").pop())}</span></td>
      ${cells(f.cost, f.own)}${nums(f)}</tr>`;
  }).join("") + `</tbody></table></div>` +
    note("«Потрачено» у папки — вместе с подпапками. Записи вне папок входят только в общий итог.");

  if (group === "users") return head("кто потратил") + d.users.map(u =>
    `<tr><td>${u.login ? `${esc(d.names[u.login] || u.login)} <span class="dim">${esc(u.login)}</span>`
                       : `<span class="dim">неизвестно — до журнала или мимо страницы</span>`}</td>
      ${cells(u.cost)}${nums(u)}</tr>`).join("") + `</tbody></table></div>` +
    note("Расход относится к тому, кто запустил обработку или пересборку; если сервис взял запись сам — к тому, кто её загрузил.");

  if (group === "providers") return head("поставщик") + d.providers.map(p =>
    `<tr><td>${p.provider ? `<b>${esc(p.provider)}</b>` : `<span class="dim">не назван</span>`}</td>
      ${cells(p.cost)}${nums(p)}</tr>`).join("") + `</tbody></table></div>` +
    note("Кому заплачено. Запись, которую начал основной поставщик, а закончил запасной, посчитана у обоих — " +
         "каждому своя часть. Расходы до появления второго поставщика отнесены к OpenRouter.");

  if (group === "records") return `<div class="scroll" style="margin-top:12px"><table><thead><tr><th>запись</th>${spentHead}
      <th class="num">звука</th><th class="num">прогонов</th></tr></thead><tbody>` +
    d.records.slice(0, RECORDS_SHOWN).map(r => {
      const cut = r.file.lastIndexOf("/");
      return `<tr><td>${esc(r.file.slice(cut + 1))}<div class="dim" style="font-size:12px">${esc(r.file.slice(0, Math.max(cut, 0)))}</div></td>
        ${cells(r.cost)}<td class="num r">${hours(r.audio_sec)}</td><td class="num r">${r.runs}</td></tr>`;
    }).join("") + `</tbody></table></div>` + (d.records.length > RECORDS_SHOWN
      ? `<div class="note" style="margin-top:12px">Показаны ${RECORDS_SHOWN} самых дорогих из ${d.records.length}. Все — в файле Excel или CSV.</div>` : "");

  return `<div class="scroll" style="margin-top:12px"><table><thead><tr><th>этап</th><th>модель</th>${spentHead}</tr></thead><tbody>` +
    d.stages.map(s => `<tr><td>${esc(STAGES[s.stage] || s.stage)}</td><td>${s.model ? esc(s.model) : `<span class="dim">—</span>`}</td>
      ${cells(s.cost)}</tr>`).join("") + `</tbody></table></div>`;
}

document.getElementById("costbar").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.id === "costprint") print();
  else if (b.dataset.cur) { cost.cur = b.dataset.cur; renderCosts(); }
  else if (b.dataset.preset) {
    cost.preset = b.dataset.preset;
    const step = PRESETS.find(p => p[0] === cost.preset)[2];
    if (step) cost.bucket = step;
    loadCosts();
  }
});
document.getElementById("costbar").addEventListener("change", e => {
  if (e.target.id === "costbucket") {
    cost.bucket = e.target.value;
    if (cost.view === "cal" && cost.bucket !== "day") cost.view = "bars";      // календарь бывает только по дням
  }
  else if (e.target.id === "coststart") { cost.start = e.target.value; cost.preset = "custom"; }
  else if (e.target.id === "costend") { cost.end = e.target.value; cost.preset = "custom"; }
  else return;
  loadCosts();
});
document.getElementById("costview").addEventListener("click", e => {
  const b = e.target.closest("button[data-view]");
  if (!b) return;
  cost.view = b.dataset.view;
  if (cost.view === "cal" && cost.bucket !== "day") loadCosts(); else renderCosts();
});
document.getElementById("costview").addEventListener("change", e => {
  if (e.target.id === "costsplit") { cost.split = e.target.value; renderCosts(); }
});
document.getElementById("costgroup").addEventListener("click", e => {
  const b = e.target.closest("button[data-group]");
  if (b) { cost.group = b.dataset.group; renderCosts(); }
});
// ширина блока изменилась — график пересчитывается под неё; печать — под ширину листа, с раскрытыми таблицами
new ResizeObserver(() => {
  const box = document.getElementById("costseries");
  if (cost.data && !costPane.hidden && !printing && box.clientWidth && +box.dataset.width !== box.clientWidth) renderSeries();
}).observe(document.getElementById("costseries"));
for (const [event, on] of [["beforeprint", true], ["afterprint", false]])
  addEventListener(event, () => { printing = on; if (cost.data && !costPane.hidden) renderCosts(); });
costRecall();

const READS = { only: "только папка", tree: "с подпапками", none: "снято" };
// право загрузки в личную папку попадает в событие, только когда его выдали или сняли
const homeUpload = e => e.home_upload === undefined ? ""
  : e.home_upload ? "; загрузка в личную папку разрешена" : "; загрузка в личную папку снята";
const ACTIONS = {
  login: e => e.source === "domain" ? "вход под учётной записью домена" : "вход",
  login_failed: e => "неудачная попытка входа" + (e.reason ? `: ${e.reason}` : ""),
  domain_user_new: e => `первый вход через домен — заведён в сервисе: ${e.name}${e.admin ? " (администратор сервиса по группе домена)" : ""}`,
  domain_role: e => e.admin ? "стал администратором сервиса: включён в группу администраторов в домене"
    : "больше не администратор сервиса: исключён из группы администраторов в домене",
  domain_recheck_denied: e => `перепроверка в домене — вход закрыт: ${e.reason}`,
  domain_bind: e => `привязал учётную запись ${e.login} к домену (пароль сервиса стёрт)`,
  domain_check: e => `проверил подключение к домену${e.login ? ` и вход пользователя ${e.login}` : ""}: ${e.ok ? "пройдена" : "есть отказы"}`,
  password: e => "сменил свой пароль" + (e.keys ? ` и отозвал свои ключи доступа: ${e.keys}` : ""),
  logout_all: () => "вышел на всех устройствах («Выйти везде»)",
  sessions_end: e => `завершил все входы пользователя ${e.login}`,
  request_known: e => `подана заявка с почтой, которая уже есть у ${e.login}: заявка не сохранена, ему ушло письмо «учётная запись уже есть»`,
  request_new: e => `подана заявка на доступ: ${e.name}, логин ${e.login}, почта ${e.email}`,
  request_approve: e => `одобрил заявку на доступ — заведён пользователь ${e.login}${
    e.home_upload ? "; загрузка в личную папку разрешена" : ""}`,
  request_reject: e => `отклонил заявку на доступ (логин ${e.login})`,
  link_sent: e => `выслана ссылка «задать пароль» для ${e.login}: ` +
    ({ invite: "доступ открыт по заявке", reset: "человек нажал «Забыл пароль»", admin: "по кнопке администратора" }[e.why] || e.why),
  link_shown: e => `выпустил ссылку «задать пароль» для ${e.login} и получил её сам, чтобы передать: ` +
    ({ invite: "доступ открыт по заявке", admin: "по кнопке в строке пользователя" }[e.why] || e.why),
  password_link: e => "задал пароль по одноразовой ссылке" + (e.keys ? ` и отозвал свои ключи доступа: ${e.keys}` : ""),
  reset_request: e => `запрошен сброс пароля для «${e.login}»`,
  upload: e => `загрузил запись «${e.file}» (${mb(e.size)})`,
  folder_create: e => `создал папку «${e.folder}»`,
  folder_rename: e => `переименовал папку «${e.folder}» в «${e.to}»`,
  folder_delete: e => `удалил папку «${e.folder}», записей в ней было ${e.records}`,
  access: e => `права на «${e.folder || "все папки"}» для ${e.login}: ` +
    [e.read && `чтение — ${READS[e.read]}`, e.upload && `загрузка — ${READS[e.upload]}`,
     e.manage && `администрирование — ${READS[e.manage]}`].filter(Boolean).join(", "),
  user_create: e => `завёл пользователя ${e.login}${e.admin ? " (администратор сервиса)" : ""}${homeUpload(e)}`,
  user_change: e => `изменил пользователя ${e.login}${e.admin ? " (администратор сервиса)" : ""}${e.password ? ", задал новый пароль" : ""}${homeUpload(e)}`,
  user_delete: e => `удалил пользователя ${e.login}`,
  names: e => `подписал говорящих в записи «${e.file}»${e.dictionary ? `; в словарь папки добавлено ФИО: ${e.dictionary}` : ""}`,
  dictionary: e => `изменил словарь ${e.file ? `записи «${e.file}»` : `папки «${e.folder}»`}: участников ${e.people}, сокращений ${e.terms}`,
  series: e => `${e.on ? "включил" : "выключил"} у папки «${e.folder}» серию совещаний`,
  previous_tasks: e => `${e.on ? "вернул" : "убрал"} раздел «Поручения прошлого совещания» в протоколе записи «${e.file}»`,
  protocol_edit: e => `поправил протокол записи «${e.file}» вручную (знаков: ${e.chars})`,
  protocol_restore: e => `вернул в протоколе записи «${e.file}» версию от ${String(e.stamp).replace(/^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d).*$/, "$3.$2.$1 $4:$5")}`,
  key_issue: e => `выпустил ключ доступа «${e.key}»${e.key_kind === "manage" ? " — с управлением сервисом" : ""}`,
  key_revoke: e => `отозвал ключ доступа «${e.key}»${e.owner ? ` пользователя ${e.owner}` : ""}`,
  speech_model: e => e.source === "env"
    ? `вернул модель распознавания к значению из .env: ${e.model} (была ${e.was})`
    : `выбрал модель распознавания для новых записей: ${e.model} (была ${e.was})`,
  backup: e => `сделал резервную копию ${e.file}${e.how === "schedule" ? " — по расписанию" : e.how === "command" ? " — командой" : ""}${
    e.inbox ? "" : ", без исходных записей"}${e.skipped ? `, не прочитано файлов: ${e.skipped}` : ""}`,
  backup_failed: e => `резервная копия не сделана${e.how === "schedule" ? " (по расписанию)" : ""}: ${e.why}`,
  backup_plan: e => e.plan.on
    ? `задал расписание резервной копии: ${e.plan.every === "week" ? `по дням «${BACKUP_DAYS[e.plan.weekday]}»` : "каждый день"} в ${
        e.plan.time}${e.plan.inbox ? "" : ", без исходных записей"}, держать копий: ${e.plan.keep}`
    : "выключил резервную копию по расписанию",
  queue_remove: e => `снял из очереди запись «${e.file}»`,
  run_cancel: e => `отменил прогон записи «${e.file}»`,
  record_rename: e => `переименовал запись «${e.file}» в «${e.to.split("/").pop()}»`,
  record_move: e => `перенёс запись «${e.file}» в папку «${e.to.split("/").slice(0, -1).join("/") || "корень"}»`,
  record_delete: e => `удалил запись «${e.file}» в корзину`,
  record_restore: e => `вернул из корзины запись «${e.file}»`,
  folder_restore: e => `вернул из корзины папку «${e.folder}», записей: ${e.records}`,
  due_mail: e => `напоминание о сроках поручений (${e.tasks}) — для ${e.to}`,
  protocol_sent: e => `отправил протокол записи «${e.file}» на почту: ${(e.to || []).join(", ")} — ` +
    ({ pdf: "вложение PDF", docx: "вложение DOCX", none: "без вложения, ссылкой" }[e.format] || e.format) +
    (e.note ? ", с припиской" : "") + ((e.refused || []).length ? `; не ушло: ${e.refused.join(", ")}` : ""),
  ready_mail: e => `письмо «${e.outcome === "ready" ? "запись готова" : "запись не обработана"}» о записи «${e.file}» — для ${e.to}`,
  mail_failed: e => (e.what === "protocol"
    ? `протокол записи «${e.file}» не отправлен на ${(e.to || []).join(", ")}`
    : e.what === "notice" ? `письмо администраторам о заявке на доступ (логин ${e.login}) не ушло`
    : e.what === "link" ? `письмо со ссылкой «задать пароль» для ${e.login} не ушло`
    : e.what === "known" ? `письмо «учётная запись уже есть» для ${e.login} не ушло`
    : e.what === "due" ? `напоминание о сроках поручений для ${e.to} не ушло`
    : `письмо о записи «${e.file}» для ${e.to} не ушло`) + `: ${e.error}`,
  notify: e => [e.ready === undefined ? "" : `${e.ready ? "включил" : "отключил"} себе письма о готовности записей`,
    e.due === undefined ? "" : `${e.due ? "включил" : "отключил"} себе напоминания о сроках поручений`].filter(Boolean).join("; "),
  task_done: e => `отметил выполненным поручение «${e.task}»${e.owner ? ` (${e.owner})` : ""} в записи «${e.file}»`,
  task_reopen: e => `снял отметку «выполнено» с поручения «${e.task}»${e.owner ? ` (${e.owner})` : ""} в записи «${e.file}»`,
  brand_logo: e => `заменил логотип организации (${e.format}, ${e.width}×${e.height})`,
  brand_name: e => e.name ? `задал название организации: «${e.name}»` : `убрал название организации (было «${e.was}»)`,
  brand_reset: () => "вернул стандартное оформление",
  template_save: e => ({ page: "изменил на странице", file: "загрузил файлом", default: "вернул стандартный текст",
                         backup: "вернул прошлую версию" }[e.how] || e.how) +
    ` — ${e.file}` + (e.warnings ? `, предупреждений: ${e.warnings}` : ""),
  type_create: e => `добавил свой тип записи «${e.title}» (${e.item})`,
  type_change: e => `изменил описание своего типа «${e.title}» (${e.item})`,
  type_delete: e => `удалил свой тип записи «${e.title}» (${e.item})`,
  task_due: e => `поменял срок поручения «${e.task}»${e.owner ? ` (${e.owner})` : ""} в записи «${e.file}»: ` +
    `${e.was || "без срока"} → ${e.now || "без срока"}`,
};

async function loadActions() {
  let events;
  try { events = (await api("/api/admin/journal?limit=200")).events; }
  catch (err) { return; }
  document.getElementById("actions").innerHTML = !events.length
    ? `<div class="empty">${icon("idle", 17)}действий пока не было</div>`
    : `<div class="scroll"><table><thead><tr><th>когда</th><th>кто</th><th>что</th></tr></thead><tbody>${
        events.map(e => `<tr><td class="num">${e.at.slice(8, 10)}.${e.at.slice(5, 7)}.${e.at.slice(0, 4)} ${e.at.slice(11, 16)}</td>
          <td>${e.by ? esc(e.by) : `<span class="dim">сервис</span>`}${
            e.via === "key" ? ` <span class="dim">· ключом «${esc(e.key || "")}»${e.key_kind === "manage" ? " (с управлением)" : ""}</span>` : ""}</td>
          <td>${esc((ACTIONS[e.kind] || (() => e.kind))(e))}</td></tr>`).join("")}</tbody></table></div>
      <div class="note" style="margin-top:12px">Последние 200 действий. Полный журнал — файл journal.jsonl в папке состояния на сервере.</div>`;
}
