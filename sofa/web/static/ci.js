/* The only script the consoles need: the theme choice, the mobile menu, and the master page's finder. Everything works without it.
   Budget: well inside the 100 KB site-shell limit. */
(function () {
  var root = document.documentElement;
  root.classList.add("js");

  // Ink (dark) is the default for operational consoles; the choice is remembered on this device.
  try {
    var saved = localStorage.getItem("ci-theme");
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) { /* storage blocked: the default stands */ }

  document.addEventListener("click", function (event) {
    var t = event.target.closest("[data-theme-toggle]");
    if (t) {
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("ci-theme", next); } catch (e) { /* ignore */ }
      t.setAttribute("aria-pressed", next === "light" ? "true" : "false");
      return;
    }
    var m = event.target.closest("[data-menu-toggle]");
    if (m) {
      var nav = document.getElementById(m.getAttribute("aria-controls"));
      var open = !nav.classList.contains("open");
      nav.classList.toggle("open", open);
      m.setAttribute("aria-expanded", open ? "true" : "false");
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      var open = document.querySelector(".nav.open");
      if (open) {
        open.classList.remove("open");
        var toggle = document.querySelector("[data-menu-toggle]");
        if (toggle) { toggle.setAttribute("aria-expanded", "false"); toggle.focus(); }
      }
    }
  });

  // The master page: type what you want to do, and the list narrows. Enter opens the first match.
  var finder = document.getElementById("finder-input");
  if (finder) {
    var items = Array.prototype.slice.call(document.querySelectorAll("[data-find]"));
    var consoles = Array.prototype.slice.call(document.querySelectorAll(".console[data-console]"));
    var count = document.getElementById("finder-count");
    var apply = function () {
      var q = finder.value.trim().toLowerCase();
      var words = q ? q.split(/\s+/) : [];
      var shown = 0;
      items.forEach(function (el) {
        var hay = el.getAttribute("data-find").toLowerCase();
        var ok = words.every(function (w) { return hay.indexOf(w) !== -1; });
        el.hidden = !ok;
        if (ok) shown += 1;
      });
      consoles.forEach(function (c) {
        c.hidden = words.length > 0 && !c.querySelector("[data-find]:not([hidden])");
      });
      if (count) count.textContent = q ? (shown + (shown === 1 ? " match" : " matches")) : "";
    };
    finder.addEventListener("input", apply);
    finder.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        var first = document.querySelector("[data-find]:not([hidden]) a");
        if (first) { event.preventDefault(); window.location.href = first.getAttribute("href"); }
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "/" && document.activeElement !== finder && !/input|textarea|select/i.test(document.activeElement.tagName)) {
        event.preventDefault();
        finder.focus();
      }
    });
  }
})();
