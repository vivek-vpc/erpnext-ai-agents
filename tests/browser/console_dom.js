"use strict";
// The penetration test of 19 Oct 2026 put hostile text (item names, vendor names, a person's full name, the chat question) into the console in a real browser and looked for script that ran
// (xss.py, X0 to X7). This is the same tour in a DOM that needs no browser: jsdom runs the real console against a running server whose data is hostile, and the page is inspected after every
// step. jsdom does not run an <img onerror> handler, so the test does not wait for a payload to fire: it looks for what a payload would leave behind (an element with an event-handler
// attribute, another script, a frame, a javascript: link) and requires the hostile text to be present as TEXT. (Content-Security-Policy is not enforced by jsdom; tests/test_hardening.py checks the header.)
//
// usage: node console_dom.js <base url>      prints one JSON array: [{name, ok, detail}]
const { JSDOM, VirtualConsole } = require("jsdom");

const base = process.argv[2];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const PAY = '<img src=x onerror="window.__xss=(window.__xss||0)+1">';
const results = [];
const note = (name, ok, detail = "") => results.push({ name, ok, detail });

async function open(storage = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", e => errors.push(String(e && e.message || e).slice(0, 200)));
  const dom = await JSDOM.fromURL(base + "/ui/", {
    runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(window) {
      window.fetch = (u, o) => fetch(new URL(u, base + "/ui/"), o);                 // jsdom has no fetch: the real one, with the page's own address as the base
      for (const [k, v] of Object.entries(storage)) window.sessionStorage.setItem(k, v);
      window.alert = () => {}; window.confirm = () => false; window.scrollTo = () => {};
      window.Element.prototype.scrollIntoView = function () {};                       // layout does not exist in jsdom
    },
  });
  await sleep(1200);
  return { dom, window: dom.window, document: dom.window.document, errors };
}

// what a payload that got into the page would leave behind
function audit(w) {
  const d = w.document;
  const found = [];
  const handlers = [...d.querySelectorAll("*")].filter(e => [...e.attributes].some(a => a.name.toLowerCase().startsWith("on")));
  if (handlers.length) found.push(`${handlers.length} element(s) with an event-handler attribute: ${handlers.slice(0, 2).map(e => e.outerHTML.slice(0, 90)).join(" | ")}`);
  if (d.scripts.length !== 1) found.push(`${d.scripts.length} scripts in the page (the console has exactly one)`);
  const frames = d.querySelectorAll("iframe, object, embed, base, applet, [srcdoc]");
  if (frames.length) found.push(`${frames.length} frame/object/base element(s)`);
  const js = [...d.querySelectorAll("[href], [src], [action], [formaction]")].filter(e => ["href", "src", "action", "formaction"].some(a => /^\s*javascript:/i.test(e.getAttribute(a) || "")));
  if (js.length) found.push(`${js.length} javascript: link(s)`);
  const imgs = [...d.querySelectorAll("img")].filter(e => !/^(data:|\/)/.test(e.getAttribute("src") || ""));
  if (imgs.length) found.push(`${imgs.length} image(s) loaded from an address that is not the page's own: ${imgs[0].outerHTML.slice(0, 80)}`);
  if (w.__xss) found.push(`window.__xss = ${w.__xss}`);
  return found;
}

const text = (w, sel) => (w.document.querySelector(sel) || { textContent: "" }).textContent;
const click = (w, sel) => { const e = w.document.querySelector(sel); if (e) e.click(); return !!e; };

async function signIn(w, user, pwd) {
  const d = w.document;
  if (!d.querySelector("#lf")) return false;
  d.querySelector("#lu").value = user;
  d.querySelector("#lp").value = pwd;
  d.querySelector("#lf").requestSubmit();
  for (let i = 0; i < 40 && d.querySelector("#app").hidden; i++) await sleep(250);
  await sleep(800);
  return !d.querySelector("#app").hidden;
}

async function tour(w, label) {
  const found = [];
  for (const view of ["ok", "chat", "stock", "buying", "history", "settings"]) {
    click(w, `#nav [data-view=${view}]`);
    await sleep(900);
    for (const f of audit(w)) found.push(`${label}, ${view} page: ${f}`);
  }
  return found;
}

(async () => {
  let { window: w, errors } = await open();
  const signed = await signIn(w, "vivek@acme.com", "pw-vivek");
  note("X0 the console loads, the person signs in through the form and the page appears", signed, signed ? "" : "the sign-in form did not lead to the app; errors: " + errors.slice(0, 3).join("; "));
  if (!signed) { console.log(JSON.stringify(results)); process.exit(0); }

  // the audit must see a payload that did get in, or every "inert" below would prove nothing
  const probe = w.document.createElement("div");
  probe.innerHTML = PAY + '<a href="javascript:alert(1)">x</a><iframe></iframe>';
  w.document.body.appendChild(probe);
  const seen = audit(w);
  probe.remove();
  note("X0 self-check: the audit finds an event handler, a javascript: link and a frame when they are there", seen.length >= 3 && audit(w).length === 0, seen.join("; ").slice(0, 300));

  click(w, "#nav [data-view=stock]"); await sleep(1000);
  const onStock = text(w, "#view").includes("<img src=x onerror=");
  click(w, "#nav [data-view=ok]"); await sleep(900);
  const onRequests = text(w, "#view").includes("<img src=x onerror=");
  note("X0 sanity: the hostile names really are on the pages, displayed as plain text", onStock && onRequests, `stock page: ${onStock}, requests page: ${onRequests}`);

  let found = await tour(w, "administrator");
  note("X1 hostile item and vendor names are shown inertly on every page", found.length === 0, found.join("; "));

  click(w, "#nav [data-view=ok]"); await sleep(800);
  [...w.document.querySelectorAll("#view [data-act=details]")].forEach(b => b.click());
  await sleep(1000);
  found = audit(w);
  note("X2 'Show all the numbers' (the details of a request) is inert", found.length === 0, found.join("; "));

  click(w, "#nav [data-view=chat]"); await sleep(900);
  for (const q of ["What is running low?", "compare vendors for RELAY-12V", "Tell me about RELAY-12V", "what is waiting for my OK", PAY, '"><svg onload=window.__xss=9>', "how much " + PAY + " do we have"]) {
    const input = w.document.querySelector("#chatq");
    if (!input) break;
    input.value = q;
    w.document.querySelector("#chatform").requestSubmit();
    await sleep(1100);
  }
  [...w.document.querySelectorAll("#msgs [data-ask]")].slice(0, 6).forEach(b => b.click());
  await sleep(1800);
  found = audit(w);
  const echoed = text(w, "#msgs").includes("<svg onload=");
  note("X3 chat answers, suggested follow-ups and the echoed question are inert", found.length === 0 && echoed, found.join("; ") || (echoed ? "" : "the hostile question was not shown at all (the check would be empty)"));

  const saved = {};
  for (let i = 0; i < w.sessionStorage.length; i++) { const k = w.sessionStorage.key(i); saved[k] = w.sessionStorage.getItem(k); }
  w.close();
  ({ window: w, errors } = await open(saved));
  click(w, "#nav [data-view=chat]"); await sleep(1200);
  found = audit(w);
  const replayed = text(w, "#msgs").includes("<svg onload=");
  note("X4 a conversation replayed from the browser's storage is inert", found.length === 0 && replayed, found.join("; ") || (replayed ? "" : "nothing was replayed (the check would be empty)"));
  note("X5 the session token lives in sessionStorage (readable by any script that did run in the page: a known property, see docs/PENTEST_REPORT.md)", !!saved.tok, `token present: ${!!saved.tok}`);

  click(w, "#signout"); await sleep(800);
  const again = await signIn(w, "evil@acme.com", "pw-evil");
  const menu = w.document.querySelector("#meName");
  const menuInert = !!menu && menu.querySelector("*") === null && menu.textContent.includes("<img src=x onerror=");
  note("X6 an ERPNext user whose full name is a payload cannot run script through the side menu", again && menuInert && audit(w).length === 0, again ? `menu shows: ${menu ? menu.innerHTML.slice(0, 80) : "nothing"}` : "the hostile user could not sign in");
  found = again ? await tour(w, "evil user") : ["could not sign in"];
  note("X7 the same person touring every page runs nothing", found.length === 0, found.join("; "));
  w.close();
  console.log(JSON.stringify(results));
})().catch(e => { console.log(JSON.stringify([...results, { name: "the tour itself", ok: false, detail: String(e && e.stack || e).slice(0, 400) }])); process.exit(0); });
