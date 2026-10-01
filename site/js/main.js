// Mycroft landing page: the findings viewer, the switch chart, the copy-address fallback, and the
// motion system. Everything here is additive; the page is complete without it. GSAP + ScrollTrigger
// drive the motion and Lenis is the only smooth-scroll engine. Under prefers-reduced-motion neither
// runs and every element is in its final state.
(function () {
  "use strict";

  var root = document.documentElement;
  var reduceQ = window.matchMedia("(prefers-reduced-motion: reduce)");
  var NAV_OFFSET = -80;
  var lenis = null;

  function $(s, c) { return (c || document).querySelector(s); }
  function $$(s, c) { return Array.prototype.slice.call((c || document).querySelectorAll(s)); }
  function onChange(q, fn) { if (q.addEventListener) q.addEventListener("change", fn); else q.addListener(fn); }
  function scrollToEl(el, offset) {
    if (lenis) lenis.scrollTo(el, { offset: offset });
    else el.scrollIntoView({ behavior: reduceQ.matches ? "auto" : "smooth", block: "start" });
  }

  /* ---------- Theme ---------- */
  function initTheme() {
    var btn = $("[data-theme-toggle]");
    if (!btn) return;
    var darkQ = window.matchMedia("(prefers-color-scheme: dark)");
    function current() { return root.getAttribute("data-theme") || (darkQ.matches ? "dark" : "light"); }
    function sync() {
      var dark = current() === "dark";
      root.setAttribute("data-theme", current());
      btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
    }
    // Follow the system until the visitor chooses.
    onChange(darkQ, function () {
      var saved = null;
      try { saved = localStorage.getItem("mycroft-theme"); } catch (e) {}
      if (!saved) { root.removeAttribute("data-theme"); sync(); }
    });
    btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("mycroft-theme", next); } catch (e) {}
      sync();
    });
    sync();
  }

  /* ---------- Nav ---------- */
  function initNav() {
    var nav = $("[data-nav]");
    var menu = $("[data-menu]");
    function onScroll() { nav.classList.toggle("is-stuck", window.scrollY > 8); }
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    if (!menu) return;
    $$("a", menu).forEach(function (a) { a.addEventListener("click", function () { menu.open = false; }); });
    document.addEventListener("click", function (e) { if (menu.open && !menu.contains(e.target)) menu.open = false; });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && menu.open) { menu.open = false; $("summary", menu).focus(); }
    });
  }

  // In-page links: scroll with Lenis when it runs, and always move focus to the target.
  function initAnchors() {
    document.addEventListener("click", function (e) {
      var a = e.target.closest && e.target.closest('a[href^="#"]');
      if (!a || a.hasAttribute("data-f") || e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey) return;
      var id = a.getAttribute("href").slice(1);
      var target = id ? document.getElementById(id) : null;
      if (!target) return;
      e.preventDefault();
      scrollToEl(target, id === "top" ? 0 : NAV_OFFSET);
      if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
      target.focus({ preventScroll: true });
      if (history.pushState) history.pushState(null, "", "#" + id);
    });
  }

  /* ---------- Copy address, query or formula ---------- */
  // mailto: does nothing for people without a mail app, so the address can always be copied.
  // data-copy-from names an element whose text is copied instead (the cache-check query).
  function initCopy() {
    var status = $("[data-copy-status]");
    $$("[data-copy]").forEach(function (btn) {
      var label = $("span", btn), original = label.textContent, timer = null;
      btn.addEventListener("click", function () {
        var from = btn.getAttribute("data-copy-from");
        var source = from ? document.getElementById(from) : null;
        var text = source ? source.textContent : btn.getAttribute("data-copy");
        function done() {
          label.textContent = "Copied";
          btn.classList.add("done");
          if (status) status.textContent = btn.getAttribute("data-copy-said") || "Email address copied";
          clearTimeout(timer);
          timer = setTimeout(function () { label.textContent = original; btn.classList.remove("done"); }, 2200);
        }
        function fallback() {
          var addr = source || btn.parentNode.querySelector(".addr");
          if (!addr) return;
          var range = document.createRange();
          range.selectNodeContents(addr);
          var sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          var ok = false;
          try { ok = document.execCommand("copy"); } catch (e) {}
          if (ok) done();
          else if (status) status.textContent = (source ? "Selected" : "Address selected") + ". Press Ctrl+C or Command+C to copy.";
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, fallback);
        } else { fallback(); }
      });
    });
  }

  /* ---------- Apply menu ---------- */
  // "Apply for a pilot" is a mailto: link, which does nothing without a desktop mail app. With
  // JavaScript it opens a menu instead: Gmail, Outlook, the mail app, or copy the address.
  function initApply() {
    var pop = $("[data-apply-pop]");
    var buttons = $$("[data-apply]");
    if (!pop || !buttons.length) return;
    var owner = null;
    var items = $$(".ap-item", pop);

    function place() {
      if (!owner) return;
      var r = owner.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) { close(false); return; }
      var w = pop.offsetWidth, h = pop.offsetHeight, gap = 8;
      var left = Math.min(Math.max(12, r.left), window.innerWidth - w - 12);
      var top = r.bottom + gap;
      if (top + h > window.innerHeight - 12 && r.top - gap - h > 12) top = r.top - gap - h;
      pop.style.left = Math.round(left) + "px";
      pop.style.top = Math.round(top) + "px";
    }
    function open(btn) {
      if (owner) owner.setAttribute("aria-expanded", "false");
      owner = btn;
      btn.setAttribute("aria-expanded", "true");
      pop.hidden = false;
      place();
      items[0].focus({ preventScroll: true });
    }
    function close(restoreFocus) {
      if (!owner) return;
      var btn = owner;
      owner = null;
      btn.setAttribute("aria-expanded", "false");
      pop.hidden = true;
      if (restoreFocus) btn.focus({ preventScroll: true });
    }

    buttons.forEach(function (btn) {
      btn.setAttribute("aria-haspopup", "true");
      btn.setAttribute("aria-expanded", "false");
      btn.setAttribute("aria-controls", "apply-pop");
      btn.addEventListener("click", function (e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        if (owner === btn) close(true); else open(btn);
      });
    });
    // Leaving to Gmail, Outlook or the mail app closes the menu; copying keeps it open to say "Copied".
    $$("a.ap-item", pop).forEach(function (a) { a.addEventListener("click", function () { close(false); }); });
    document.addEventListener("click", function (e) {
      if (owner && !pop.contains(e.target) && !owner.contains(e.target)) close(false);
    });
    document.addEventListener("keydown", function (e) {
      if (!owner) return;
      if (e.key === "Escape") { e.preventDefault(); close(true); return; }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        var i = items.indexOf(document.activeElement);
        i = e.key === "ArrowDown" ? (i + 1) % items.length : (i - 1 + items.length) % items.length;
        items[i].focus();
      }
    });
    pop.addEventListener("focusout", function (e) {
      if (owner && e.relatedTarget && !pop.contains(e.relatedTarget) && e.relatedTarget !== owner) close(false);
    });
    window.addEventListener("scroll", place, { passive: true });
    window.addEventListener("resize", place, { passive: true });
  }

  /* ---------- Findings viewer ---------- */
  // Without JavaScript every finding is shown in full and the list items are plain anchors.
  var onFinding = [];
  function initFindings() {
    var box = $("[data-findings]");
    if (!box) return;
    var items = $$("[data-f]", box), details = $$("[data-fd]", box);
    var narrow = window.matchMedia("(max-width: 900px)");
    function open(n, fromClick) {
      items.forEach(function (a) {
        if (a.getAttribute("data-f") === n) a.setAttribute("aria-current", "true");
        else a.removeAttribute("aria-current");
      });
      var shown = null;
      details.forEach(function (d) {
        var on = d.getAttribute("data-fd") === n;
        d.classList.toggle("is-open", on);
        if (on) shown = d;
      });
      onFinding.forEach(function (fn) { fn(shown); });
      if (fromClick && narrow.matches && shown) scrollToEl(shown, NAV_OFFSET);
    }
    items.forEach(function (a) {
      a.addEventListener("click", function (e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        open(a.getAttribute("data-f"), true);
      });
    });
    open("2", false);
  }

  /* ---------- Switch chart ---------- */
  // Values come from data attributes written by site-src/build.py from the engine's numbers.
  var chart = null;
  function initChart() {
    var fig = $("[data-instrument]");
    if (!fig) return;
    var d = fig.dataset;
    var X0 = +d.x0, X1 = +d.x1, Y0 = +d.y0, YT = +d.ytop, VMAX = +d.vmax;
    var STAY = +d.stay, FIRST = +d.first, WARM = +d.warm, ALT = +d.alt;
    function x(n) { return X0 + (X1 - X0) * n / 10; }
    function y(v) { return Y0 - (Y0 - YT) * Math.min(v, VMAX) / VMAX; }
    function stay(n) { return STAY * n; }
    function sw(n) { return n <= 1 ? FIRST * n : FIRST + WARM * (n - 1); }
    function fmt(v) { return "$" + v.toFixed(3); }

    var svg = $("svg", fig);
    var guide = $(".guide", fig), dStay = $(".dot.stay", fig), dSw = $(".dot.switch", fig);
    var input = $("[data-scrub]", fig);
    var ro = {};
    $$("[data-ro]", fig).forEach(function (el) { ro[el.getAttribute("data-ro")] = el; });
    var state = { n: 6, shown: 0 };
    var tween = null;

    function verdict(n) {
      var a = stay(n), b = sw(n);
      if (n === 1) return "The first request after switching costs <b>3.9x</b> what staying would have.";
      if (b > a) return "Switching is still <b>behind by " + fmt(b - a) + "</b>. It pays back on request 6.";
      return "Switching is <b>ahead by " + fmt(a - b) + "</b>. It overtook staying on request 6.";
    }
    function render(nf) {
      var gx = x(nf).toFixed(1);
      guide.setAttribute("x1", gx); guide.setAttribute("x2", gx);
      dStay.setAttribute("cx", gx); dStay.setAttribute("cy", y(stay(nf)).toFixed(1));
      dSw.setAttribute("cx", gx); dSw.setAttribute("cy", y(sw(nf)).toFixed(1));
      var n = Math.min(10, Math.max(1, Math.round(nf)));
      if (n === state.shown) return;
      state.shown = n;
      ro.n.textContent = n;
      ro.stay.textContent = fmt(stay(n));
      ro.switch.textContent = fmt(sw(n));
      ro.alt.textContent = fmt(ALT * n);
      ro.verdict.innerHTML = verdict(n);
      input.value = n;
      input.setAttribute("aria-valuetext", "Request " + n + ": staying " + fmt(stay(n)) +
        ", switching " + fmt(sw(n)) + ", alternating " + fmt(ALT * n));
    }
    function go(n) {
      if (tween) tween.kill();
      if (window.gsap && !reduceQ.matches) {
        tween = gsap.to(state, { n: n, duration: 0.45, ease: "power3.out", onUpdate: function () { render(state.n); } });
      } else { state.n = n; render(n); }
    }
    function fromPointer(e) {
      var r = svg.getBoundingClientRect();
      var vx = (e.clientX - r.left) / r.width * 720;
      return Math.min(10, Math.max(1, Math.round((vx - X0) / (X1 - X0) * 10)));
    }
    var plot = $("[data-plot]", fig);
    plot.addEventListener("pointermove", function (e) {
      if (e.pointerType === "mouse" || e.buttons) { var n = fromPointer(e); if (n !== Math.round(state.n)) go(n); }
    });
    plot.addEventListener("pointerdown", function (e) { go(fromPointer(e)); });
    input.addEventListener("input", function () { go(+input.value); });
    render(6);
    chart = {
      fig: fig,
      // Used by the chart's entrance: walk the guide from request 1 to 6 once the lines have drawn.
      sweep: function (tl, at) {
        tl.call(function () { state.n = 1; render(1); }, null, at);
        tl.to(state, { n: 6, duration: 1.6, ease: "power2.inOut", onUpdate: function () { render(state.n); } }, at);
      }
    };
  }

  /* ---------- Road fill length (up to the "Now" node) ---------- */
  function sizeRoad() {
    var road = $("[data-road]"), fill = $("[data-road-fill]"), now = $(".now .node", road);
    if (!road || !fill || !now) return;
    var r = road.getBoundingClientRect(), n = now.getBoundingClientRect();
    if (window.matchMedia("(min-width: 901px)").matches) {
      fill.style.height = ""; fill.style.width = Math.round(n.left - r.left + n.width / 2) + "px";
    } else {
      fill.style.width = ""; fill.style.height = Math.round(n.top - r.top + n.height / 2) + "px";
    }
  }

  /* ---------- Split text ---------- */
  // Words are wrapped for a masked rise. The visible split copy is aria-hidden; an unsplit copy
  // stays in the element for assistive technology. Inline elements (em, span) are preserved.
  function split(el) {
    if (el._words) return el._words;
    var text = el.textContent.replace(/\s+/g, " ").trim();
    var vis = document.createElement("span");
    vis.className = "split";
    vis.setAttribute("aria-hidden", "true");
    while (el.firstChild) vis.appendChild(el.firstChild);
    (function walk(node) {
      Array.prototype.slice.call(node.childNodes).forEach(function (c) {
        if (c.nodeType === 3) {
          var frag = document.createDocumentFragment();
          c.nodeValue.split(/(\s+)/).forEach(function (part) {
            if (!part) return;
            if (/^\s+$/.test(part)) { frag.appendChild(document.createTextNode(" ")); return; }
            var w = document.createElement("span"), wi = document.createElement("span");
            w.className = "w"; wi.className = "wi"; wi.textContent = part;
            w.appendChild(wi); frag.appendChild(w);
          });
          node.replaceChild(frag, c);
        } else if (c.nodeType === 1) { walk(c); }
      });
    })(vis);
    var sr = document.createElement("span");
    sr.className = "sr-only";
    sr.textContent = text;
    el.appendChild(sr);
    el.appendChild(vis);
    el._words = $$(".wi", vis);
    return el._words;
  }

  /* ---------- Motion ---------- */
  function initMotion() {
    var gsap = window.gsap, ST = window.ScrollTrigger;
    if (!gsap || !ST) { root.classList.remove("motion"); return; }
    gsap.registerPlugin(ST);
    gsap.defaults({ ease: "power3.out", duration: 0.9 });

    var mm = gsap.matchMedia();
    mm.add("(prefers-reduced-motion: no-preference)", function () {
      root.classList.add("motion", "motion-live");

      // Lenis, driven by the GSAP ticker so ScrollTrigger and smooth scroll share one clock.
      var tick = null;
      if (window.Lenis) {
        lenis = new window.Lenis({ lerp: 0.1, smoothWheel: true, wheelMultiplier: 0.95 });
        lenis.on("scroll", ST.update);
        tick = function (t) { lenis.raf(t * 1000); };
        gsap.ticker.add(tick);
        gsap.ticker.lagSmoothing(0);
      }

      // Play a paused tween the first time its trigger comes into view. The tween is not owned by
      // the ScrollTrigger, so a fast scroll straight past the trigger still plays it to the end.
      function onceInView(trigger, tween, start) {
        var st = ST.create({
          trigger: trigger, start: start || "top 86%",
          onEnter: function () { tween.play(); st.kill(); }
        });
        if (st.progress > 0) { tween.play(); st.kill(); }
      }

      // Hero: the headline word by word, then the report window and its breakdowns.
      var tl = gsap.timeline({ defaults: { ease: "expo.out" } });
      var h1 = $("[data-hero]");
      if (h1) {
        var hw = split(h1);
        gsap.set(h1, { visibility: "visible" });
        tl.from(hw, { yPercent: 110, duration: 1.1, stagger: 0.045 }, 0.05);
      }
      var hwin = $("[data-hero-win]");
      if (hwin) {
        tl.from(hwin, { y: 40, autoAlpha: 0, duration: 1.2 }, 0.3)
          .from($$("[data-reveal-hero]", hwin), { y: 14, autoAlpha: 0, duration: 0.8, stagger: 0.1, ease: "power3.out" }, 0.7)
          .from($$("[data-bar]", hwin), { scaleX: 0, duration: 1, stagger: 0.04, ease: "expo.out" }, 0.8);
      }

      // Section headings: word by word, once.
      $$("[data-split]:not([data-hero])").forEach(function (el) {
        var words = split(el);
        gsap.set(el, { visibility: "visible" });
        onceInView(el, gsap.from(words, { yPercent: 110, duration: 1.05, ease: "expo.out", stagger: 0.045, paused: true }));
      });

      // Supporting copy and media, in reading order.
      gsap.set("[data-reveal]", { y: 22 });
      ST.batch("[data-reveal]", {
        start: "top 90%", once: true,
        onEnter: function (batch) { gsap.to(batch, { autoAlpha: 1, y: 0, duration: 0.9, stagger: 0.08, overwrite: true }); }
      });

      // The findings window settles flat as it arrives; switching findings eases the new one in.
      var fwin = $("[data-findings-win]");
      if (fwin) {
        gsap.fromTo(fwin, { rotateX: 10, y: 60, scale: 0.96, transformPerspective: 1800 }, {
          rotateX: 0, y: 0, scale: 1, ease: "none",
          scrollTrigger: { trigger: fwin.parentNode, start: "top 96%", end: "top 30%", scrub: 0.9 }
        });
      }
      onFinding.push(function (detail) {
        if (!detail) return;
        gsap.fromTo(detail.children, { y: 10, autoAlpha: 0 }, { y: 0, autoAlpha: 1, duration: 0.5, stagger: 0.05, overwrite: true });
        var fills = $$(".fill", detail);
        if (fills.length) gsap.fromTo(fills, { scaleX: 0 }, { scaleX: 1, duration: 0.9, stagger: 0.05, ease: "expo.out" });
      });

      // Cache meters fill once.
      $$(".meter").forEach(function (m) {
        onceInView(m, gsap.from($$("[data-bar]", m), { scaleX: 0, duration: 1.4, ease: "expo.out", paused: true, delay: 0.2 }), "top 82%");
      });

      // The switch chart draws itself when it comes into view, then the guide walks to the payback.
      if (chart) {
        var fig = chart.fig;
        var ctl = gsap.timeline({ paused: true, defaults: { ease: "expo.out" } });
        var lines = $$("[data-line]", fig).map(function (p) {
          var len = p.getTotalLength();
          gsap.set(p, { strokeDasharray: len, strokeDashoffset: len });
          return p;
        });
        ctl.from($$(".gl, .gt, .gb", fig), { autoAlpha: 0, duration: 0.6, stagger: 0.01, ease: "power2.out" }, 0)
          .to(lines[1], { strokeDashoffset: 0, duration: 1.1, ease: "power2.inOut" }, 0.2)   // stay
          .to(lines[2], { strokeDashoffset: 0, duration: 1.3, ease: "power2.inOut" }, 0.3)   // switch
          .to(lines[0], { strokeDashoffset: 0, duration: 0.55, ease: "power2.in" }, 0.75)    // alternate
          .set(lines, { clearProps: "strokeDasharray,strokeDashoffset" })
          .from($("[data-alt-note]", fig), { autoAlpha: 0, y: 6, duration: 0.6 }, 1.25)
          .from($("[data-guide]", fig), { autoAlpha: 0, duration: 0.4 }, 0.5);
        chart.sweep(ctl, 0.55);
        ctl.from($("[data-pay]", fig), { autoAlpha: 0, scale: 0.6, transformOrigin: "50% 20%", duration: 0.7, ease: "back.out(2)" }, 2.0);
        onceInView(fig, ctl, "top 75%");
        // Any interaction ends the entrance where it is.
        ["pointerdown", "keydown"].forEach(function (ev) {
          fig.addEventListener(ev, function () { if (ctl.isActive() || ctl.progress() === 0) ctl.progress(1); }, { once: true });
        });
      }

      // The statement: words light up as it is read, tied to scroll.
      var st = $("[data-scrub-words]");
      if (st) {
        var sw = split(st);
        gsap.fromTo(sw, { opacity: 0.14 }, {
          opacity: 1, ease: "none", stagger: 0.12,
          scrollTrigger: { trigger: st, start: "top 82%", end: "bottom 50%", scrub: 0.9 }
        });
      }

      // Connection check rows tick in once.
      var checks = $("[data-checks]");
      if (checks) onceInView(checks, gsap.from($$("li", checks), { autoAlpha: 0, x: -10, duration: 0.5, stagger: 0.06, paused: true }), "top 80%");

      // Roadmap: the rail fills up to "Now" as the section is read.
      var fill = $("[data-road-fill]");
      if (fill) {
        var horizontal = window.matchMedia("(min-width: 901px)").matches;
        gsap.fromTo(fill, horizontal ? { scaleX: 0 } : { scaleY: 0 }, {
          scaleX: 1, scaleY: 1, ease: "none",
          scrollTrigger: { trigger: "[data-road]", start: "top 85%", end: horizontal ? "top 45%" : "center 55%", scrub: 0.9 }
        });
      }

      return function () {
        root.classList.remove("motion");
        if (tick) gsap.ticker.remove(tick);
        if (lenis) { lenis.destroy(); lenis = null; }
      };
    });

    ST.addEventListener("refreshInit", sizeRoad);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { ST.refresh(); });
    window.addEventListener("load", function () { ST.refresh(); });
  }

  function init() {
    initTheme();
    initNav();
    initAnchors();
    initCopy();
    initApply();
    initFindings();
    initChart();
    sizeRoad();
    window.addEventListener("resize", sizeRoad, { passive: true });
    initMotion();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
