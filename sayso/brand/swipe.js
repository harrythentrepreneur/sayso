/* Sayso swipe deck (shared by the self-hosted console and Sayso Cloud).
 *
 * The page renders each open decision as a normal <form class="s-card-swipe"> with Yes and No submit
 * buttons, bound to the exact fingerprint shown. Without this script those buttons simply work. With it,
 * the top card can be dragged: right = Yes, left = No. A released swipe posts the SAME form (same csrf,
 * key and fingerprint) with fetch and the header X-Sayso-Swipe: 1, and the server answers JSON
 * {"ok": true} or {"error": "why"} with a 4xx; every server rule is unchanged. If the server refuses (changed draft,
 * already decided, signed out), the card springs back and the refusal is shown; nothing is assumed.
 *
 * No libraries, no inline script, no third-party requests: the page's CSP only needs script-src 'self'
 * and connect-src 'self'.
 */
(function () {
  "use strict";
  var deck = document.querySelector("[data-sayso-deck]");
  if (!deck || !window.fetch) return;
  var THRESHOLD = 0.32;      // share of card width that commits a slow drag
  var FLICK = 0.55;          // px per ms that commits a short fast flick
  var msg = deck.querySelector("[data-deck-msg]");
  var left = deck.querySelector("[data-deck-left]");
  var busy = false;

  function cards() { return Array.prototype.slice.call(deck.querySelectorAll(".s-card-swipe:not(.is-gone)")); }
  function top() { return cards()[0] || null; }
  function say(text, bad) {
    if (!msg) return;
    msg.textContent = text || "";
    msg.classList.toggle("is-bad", !!bad);
  }
  function restack() {
    var cs = cards();
    cs.forEach(function (c, i) {
      c.style.zIndex = String(100 - i);
      c.classList.toggle("is-top", i === 0);
      c.classList.toggle("is-next", i === 1);
      c.setAttribute("aria-hidden", i === 0 ? "false" : "true");
      // Only the top card can be reached: cards under it (even the visible next one) take no focus or clicks.
      c.inert = i !== 0;
      if (i === 0) c.removeAttribute("inert"); else c.setAttribute("inert", "");
      if (!c.classList.contains("is-dragging")) {
        c.style.transform = i === 0 ? "" : "translateY(" + Math.min(i, 2) * 10 + "px) scale(" + (1 - Math.min(i, 2) * 0.04) + ")";
      }
    });
    if (left) left.textContent = cs.length ? cs.length + " left" : "";
    if (!cs.length) {
      var done = deck.querySelector("[data-deck-done]");
      if (done) done.hidden = false;
    }
  }

  function stamps(card, dx, w) {
    var p = Math.max(-1, Math.min(1, dx / (w * THRESHOLD)));
    var yes = card.querySelector(".s-stamp-yes"), no = card.querySelector(".s-stamp-no");
    if (yes) yes.style.opacity = String(Math.max(0, p));
    if (no) no.style.opacity = String(Math.max(0, -p));
    card.style.setProperty("--glow", String(Math.abs(p)));
    card.classList.toggle("is-yes", p > 0.15);
    card.classList.toggle("is-no", p < -0.15);
  }

  function springBack(card) {
    card.classList.remove("is-dragging");
    card.style.transition = "transform .28s cubic-bezier(.2,.9,.3,1.3)";
    card.style.transform = "";
    stamps(card, 0, 1);
    setTimeout(function () { card.style.transition = ""; }, 300);
  }

  function post(card, answer) {
    var data = new URLSearchParams(new FormData(card));
    data.set("answer", answer);
    return fetch(card.getAttribute("action"), {
      method: "POST", body: data, credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "X-Sayso-Swipe": "1" }
    }).then(function (r) {
      // Contract: with X-Sayso-Swipe the server answers JSON {"ok":true} or {"error":"why"} with a 4xx.
      return r.json().then(function (j) {
        if (r.ok && j && j.ok) return null;
        return (j && j.error) || ("Not recorded (HTTP " + r.status + "). Reload and look again.");
      }, function () { return "Not recorded (HTTP " + r.status + "). Reload and look again."; });
    }, function () { return "Network problem. Nothing was recorded; try again."; });
  }

  function commit(card, answer, dx) {
    if (busy) return;
    busy = true;
    var w = card.offsetWidth || 360;
    card.classList.remove("is-dragging");
    card.classList.add("is-leaving");
    card.style.transition = "transform .32s ease-in, opacity .32s ease-in";
    var dir = answer === "yes" ? 1 : -1;
    card.style.transform = "translate(" + dir * (w * 1.4) + "px," + (dx ? 0 : -20) + "px) rotate(" + dir * 18 + "deg)";
    card.style.opacity = "0";
    say(answer === "yes" ? "Saying Yes..." : "Saying No...");
    post(card, answer).then(function (err) {
      busy = false;
      card.style.transition = "";
      if (err) {
        card.classList.remove("is-leaving");
        card.style.opacity = "";
        springBack(card);
        say(err, true);
        return;
      }
      card.classList.add("is-gone");
      card.classList.remove("is-top", "is-next", "is-yes", "is-no");
      card.hidden = true;
      say(answer === "yes" ? "Yes counted. It acts on the next run, with every safety check."
                           : "No counted. Nothing is sent.");
      restack();
    });
  }

  function bind(card) {
    var down = false, sx = 0, sy = 0, dx = 0, dy = 0, t0 = 0, lock = null, grabTop = true, id = null;
    card.addEventListener("pointerdown", function (ev) {
      if (busy || !card.classList.contains("is-top")) return;
      if (ev.target.closest("button,a,input,textarea,select,summary,[data-no-drag]")) return;
      down = true; lock = null; sx = ev.clientX; sy = ev.clientY; dx = dy = 0; t0 = performance.now();
      var r = card.getBoundingClientRect();
      grabTop = ev.clientY < r.top + r.height / 2;
      id = ev.pointerId;
    });
    card.addEventListener("pointermove", function (ev) {
      if (!down || ev.pointerId !== id) return;
      dx = ev.clientX - sx; dy = ev.clientY - sy;
      if (lock === null && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
        lock = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
        if (lock === "x") { try { card.setPointerCapture(id); } catch (e) { /* old browsers */ } card.classList.add("is-dragging"); }
      }
      if (lock !== "x") return;           // vertical: let the card's text scroll
      ev.preventDefault();
      var rot = (dx / (card.offsetWidth || 360)) * 14 * (grabTop ? 1 : -1);
      card.style.transform = "translate(" + dx + "px," + dy * 0.15 + "px) rotate(" + rot + "deg)";
      stamps(card, dx, card.offsetWidth || 360);
    });
    function end(ev) {
      if (!down || (ev && ev.pointerId !== id)) return;
      down = false;
      if (lock !== "x") return;
      var w = card.offsetWidth || 360, dt = Math.max(1, performance.now() - t0), v = Math.abs(dx) / dt;
      if (Math.abs(dx) > w * THRESHOLD || (v > FLICK && Math.abs(dx) > 40)) commit(card, dx > 0 ? "yes" : "no", dx);
      else springBack(card);
    }
    card.addEventListener("pointerup", end);
    card.addEventListener("pointercancel", end);
    // The real buttons go through the same path, so a tap and a swipe behave the same.
    card.addEventListener("submit", function (ev) {
      var b = ev.submitter;
      if (!b || !b.value) return;          // let the browser handle anything unexpected
      ev.preventDefault();
      commit(card, b.value === "yes" ? "yes" : "no", 0);
    });
  }

  document.addEventListener("keydown", function (ev) {
    if (ev.target.closest && ev.target.closest("input,textarea,select")) return;
    var c = top();
    if (!c) return;
    if (ev.key === "ArrowRight") { ev.preventDefault(); commit(c, "yes", 0); }
    if (ev.key === "ArrowLeft") { ev.preventDefault(); commit(c, "no", 0); }
  });

  deck.classList.add("is-live");
  cards().forEach(bind);
  restack();
  // Small screens: bring the deck into view under any sticky header, then let the card's text area give up
  // height until the top card's Yes / No fit. Never runs if the user has already scrolled.
  fit();

  function stickyBottom() {
    var b = 0;
    Array.prototype.forEach.call(document.querySelectorAll("body *"), function (el) {
      var p = getComputedStyle(el).position;
      if (p !== "sticky" && p !== "fixed") return;
      if (deck.contains(el) || el.contains(deck)) return;
      var r = el.getBoundingClientRect();
      if (r.top <= 1 && r.bottom > b && r.height < window.innerHeight / 2) b = r.bottom;
    });
    return b;
  }

  function fit() {
    var first = top();
    if (!first || window.scrollY !== 0 || first.getBoundingClientRect().bottom <= window.innerHeight) return;
    var head = stickyBottom();
    var y = deck.getBoundingClientRect().top + window.scrollY - head - 8;
    if (y > 0) window.scrollTo(0, y);
    var over = first.getBoundingClientRect().bottom - window.innerHeight + 8;
    if (over > 0) {
      deck.querySelectorAll(".s-card-body").forEach(function (b) {
        b.style.maxHeight = Math.max(96, b.clientHeight - over) + "px";
      });
    }
  }
})();
