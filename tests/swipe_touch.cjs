// Real-touch check for the swipe deck (run by tests/test_swipe.py; needs playwright + Chrome).
// usage: node swipe_touch.cjs <chrome> <url> <cookie-name> <cookie-value> <x|y>
// Opens the deck in a 390x844 phone context with touch, then drags with CDP Input.dispatchTouchEvent
// starting at the centre of the top card's .s-card-body. Prints one JSON line with what happened.
"use strict";
const { chromium } = require("playwright");

const [chrome, url, cname, cvalue, axis] = process.argv.slice(2);

(async () => {
  const browser = await chromium.launch({ executablePath: chrome, headless: true });
  try {
    const ctx = await browser.newContext({
      viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true,
      userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    });
    const u = new URL(url);
    await ctx.addCookies([{ name: cname, value: cvalue, domain: u.hostname, path: "/" }]);
    const page = await ctx.newPage();
    const events = [];
    await page.exposeFunction("__sayso_log", (t) => { events.push(t); });
    await page.goto(url);
    await page.waitForSelector(".s-deck.is-live .s-card-swipe.is-top");
    // Give the body enough text to scroll, like a real long customer email.
    const geo = await page.evaluate(() => {
      const card = document.querySelector(".s-card-swipe.is-top");
      const body = card.querySelector(".s-card-body");
      const filler = document.createElement("div");
      for (let i = 0; i < 60; i++) {
        const p = document.createElement("p");
        p.textContent = "Line " + i + " of a long customer email so the card body has to scroll.";
        filler.appendChild(p);
      }
      body.appendChild(filler);
      body.style.maxHeight = "420px";
      ["pointerdown", "pointermove", "pointerup", "pointercancel"].forEach((t) =>
        card.addEventListener(t, (e) => window.__sayso_log(e.type), true));
      const r = body.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return { x: r.left + r.width / 2, y: r.top + r.height / 2, h: body.scrollHeight, ch: body.clientHeight,
               bodyTouch: getComputedStyle(body).touchAction, hitTouch: getComputedStyle(hit).touchAction,
               hitInBody: body.contains(hit) && hit !== body,
               key: card.querySelector("input[name=key]").value };
    });
    const cdp = await ctx.newCDPSession(page);
    const touch = (type, x, y) => cdp.send("Input.dispatchTouchEvent", {
      type, touchPoints: type === "touchEnd" ? [] : [{ x, y, id: 1, radiusX: 2, radiusY: 2, force: 1 }],
    });
    const steps = 14;
    const [ex, ey] = axis === "x" ? [geo.x + 260, geo.y] : [geo.x, geo.y - 260];
    await touch("touchStart", geo.x, geo.y);
    for (let i = 1; i <= steps; i++) {
      await touch("touchMove", geo.x + ((ex - geo.x) * i) / steps, geo.y + ((ey - geo.y) * i) / steps);
      await page.waitForTimeout(16);
    }
    const during = await page.evaluate(() => {
      const card = document.querySelector(".s-card-swipe.is-top");
      return { transform: card.style.transform, scrollTop: card.querySelector(".s-card-body").scrollTop };
    });
    await touch("touchEnd", ex, ey);
    await page.waitForTimeout(400);
    // A released swipe posts and waits for the server; wait for its answer (no answer for a scroll).
    await page.waitForFunction(() => !/^Saying /.test((document.querySelector("[data-deck-msg]") || {}).textContent || ""),
                               null, { timeout: 20000 });
    const after = await page.evaluate(() => ({
      msg: (document.querySelector("[data-deck-msg]") || {}).textContent || "",
      gone: document.querySelectorAll(".s-card-swipe.is-gone").length,

    }));
    after.events = events;
    process.stdout.write(JSON.stringify({ geo, during, after }) + "\n");
  } finally {
    await browser.close();
  }
})().catch((e) => { process.stderr.write(String(e && e.stack || e) + "\n"); process.exit(2); });
