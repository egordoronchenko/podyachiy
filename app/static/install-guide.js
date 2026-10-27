(function () {
  var toc = document.getElementById("toc"), wide = window.matchMedia("(min-width: 62.01rem)");
  function fit() { toc.open = wide.matches; }
  fit();
  if (wide.addEventListener) wide.addEventListener("change", fit);
  toc.addEventListener("click", function (e) { if (e.target.closest("a") && !wide.matches) toc.open = false; });

  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (done, fail) {
      var box = document.createElement("textarea");
      box.value = text; box.setAttribute("readonly", ""); box.style.position = "fixed"; box.style.opacity = "0";
      document.body.appendChild(box); box.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(box);
      ok ? done() : fail();
    });
  }
  document.querySelectorAll(".copy").forEach(function (btn) {
    btn.hidden = false;
    btn.addEventListener("click", function () {
      var code = btn.closest(".code").querySelector("code").textContent;
      copy(code).then(function () { say("Скопировано", true); }, function () { say("Не вышло — выделите текст", false); });
      function say(text, ok) {
        btn.textContent = text; btn.classList.toggle("done", ok);
        setTimeout(function () { btn.textContent = "Скопировать"; btn.classList.remove("done"); }, 2000);
      }
    });
  });

  // снимок экрана по щелчку открывается в полный размер: на телефоне его можно двигать пальцем
  var shots = document.querySelectorAll("figure img");
  if (shots.length && window.HTMLDialogElement) {
    document.documentElement.classList.add("js");
    var box = document.createElement("dialog"), big = document.createElement("img"), shut = document.createElement("button");
    box.className = "zoom"; shut.type = "button"; shut.setAttribute("aria-label", "Закрыть снимок"); shut.textContent = "×";
    box.appendChild(shut); box.appendChild(big); document.body.appendChild(box);
    box.addEventListener("click", function () { box.close(); });
    shots.forEach(function (img) {
      img.tabIndex = 0; img.setAttribute("role", "button"); img.setAttribute("aria-label", "Увеличить снимок: " + img.alt);
      function show() { big.src = img.src; big.alt = img.alt; big.width = img.naturalWidth; big.height = img.naturalHeight; box.showModal(); }
      img.addEventListener("click", show);
      img.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); show(); } });
    });
  }

  // в оглавлении подсвечен раздел, который сейчас на экране
  if (!("IntersectionObserver" in window)) return;
  var links = {}, current = null;
  toc.querySelectorAll("a").forEach(function (a) { links[decodeURIComponent(a.getAttribute("href").slice(1))] = a; });
  var seen = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      if (current) { current.classList.remove("on"); current.removeAttribute("aria-current"); }
      current = links[entry.target.id];
      if (!current) return;
      current.classList.add("on"); current.setAttribute("aria-current", "location");
      // на широком экране оглавление стоит на месте и прокручивается само: держим текущий пункт на виду
      if (wide.matches) {
        var top = current.offsetTop - toc.offsetTop;
        if (top < toc.scrollTop + 48 || top > toc.scrollTop + toc.clientHeight - 48) toc.scrollTop = top - toc.clientHeight / 2;
      }
    });
  }, { rootMargin: "0px 0px -75% 0px" });
  document.querySelectorAll("main h2[id], main h3[id]").forEach(function (h) { seen.observe(h); });
})();
