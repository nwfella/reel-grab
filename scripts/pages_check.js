#!/usr/bin/env node
/*
 * pages_check.js — proves the GitHub Pages artefact actually drives the local engine.
 *
 *   node scripts/pages_check.js                 # serves docs/ locally, drives that copy
 *   node scripts/pages_check.js --pages=https://nwfella.github.io/reel-grab/
 *
 * Starts reel-grab with the page's origin on the CORS allowlist, opens the published page
 * in a real browser, pastes the engine URL + token into the Connect card, and then runs a
 * real Fetch and a real Download through the remote UI. This is the only test that
 * exercises the cross-origin path (preflight + allowlist + PNA opt-in) that the published
 * page depends on.
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const net = require("net");
const { spawn } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const APP = path.join(ROOT, "reel_grab.py");
const DOCS = path.join(ROOT, "docs");
const OUT = path.join(ROOT, "pages-out");
const CC_VIDEO = "https://www.youtube.com/watch?v=YE7VzlLtp-4"; // CC-BY (Blender)

const arg = (n, d) => { const h = process.argv.find((a) => a.startsWith("--" + n + "=")); return h ? h.slice(n.length + 3) : d; };
const LIVE = arg("pages", null);

let PASS = 0, FAIL = 0; const failures = [];
const ok = (n, d = "") => { PASS++; console.log(`  \x1b[32mPASS\x1b[0m ${n}${d ? "  (" + d + ")" : ""}`); };
const bad = (n, d = "") => { FAIL++; failures.push(`${n}: ${d}`); console.log(`  \x1b[31mFAIL\x1b[0m ${n}${d ? "  (" + d + ")" : ""}`); };
const check = (c, n, d = "") => { c ? ok(n, d) : bad(n, d); return !!c; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const freePort = () => new Promise((res) => { const s = net.createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => res(p)); }); });

function findChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const home = os.homedir();
  return ["C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    path.join(home, "AppData/Local/Google/Chrome/Application/chrome.exe"),
    "/usr/bin/google-chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].find((c) => fs.existsSync(c)) || null;
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.seq = 0; this.p = new Map(); this.ls = [];
    ws.addEventListener("message", (ev) => {
      let m; try { m = JSON.parse(typeof ev.data === "string" ? ev.data : String(ev.data)); } catch (e) { return; }
      if (m.id && this.p.has(m.id)) { const { resolve, reject } = this.p.get(m.id); this.p.delete(m.id); m.error ? reject(new Error(m.error.message)) : resolve(m.result); }
      else if (m.method) for (const l of this.ls) l(m);
    });
  }
  on(f) { this.ls.push(f); }
  send(method, params) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.p.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => { if (this.p.has(id)) { this.p.delete(id); reject(new Error("CDP timeout: " + method)); } }, 30000);
    });
  }
  async eval(expression, awaitPromise) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: !!awaitPromise, userGesture: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
}
async function waitFor(fn, ms, label) { const t0 = Date.now(); for (;;) { let v = false; try { v = await fn(); } catch (e) {} if (v) return v; if (Date.now() - t0 > ms) throw new Error("timed out after " + ms + "ms waiting for " + label); await sleep(150); } }

async function main() {
  const chrome = findChrome();
  if (!chrome) { console.log("harness error: no Chrome found"); process.exit(1); }

  const appPort = await freePort();
  const staticPort = LIVE ? null : await freePort();
  const cdpPort = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "rg-pages-"));
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(OUT, { recursive: true });

  const pageUrl = LIVE || `http://127.0.0.1:${staticPort}/index.html`;
  const pageOrigin = new URL(pageUrl).origin;
  console.log(`\nreel-grab published-page gate\n  page    ${pageUrl}\n  origin  ${pageOrigin}\n  app     http://127.0.0.1:${appPort}\n`);

  const procs = [];
  const cleanup = async () => { for (const p of procs) { try { p.kill(); } catch (e) {} } await sleep(500); try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {} };

  let session = null;
  try {
    if (!LIVE) {
      const staticSrv = spawn(process.env.PYTHON || "python", ["-m", "http.server", String(staticPort), "--bind", "127.0.0.1", "--directory", DOCS], { stdio: "ignore" });
      procs.push(staticSrv);
      await waitFor(async () => (await fetch(pageUrl).catch(() => null))?.ok, 15000, "the docs server");
      ok("docs/ served over http", pageUrl);
    }

    const app = spawn(process.env.PYTHON || "python", ["-u", APP, "--port", String(appPort), "--no-browser",
      "--engine-home", path.join(os.homedir(), ".reel-grab"), "--allow-origin", pageOrigin],
      { stdio: ["ignore", "pipe", "pipe"] });
    procs.push(app);
    let token = null, log = "";
    app.stdout.on("data", (b) => { log += b.toString(); const m = log.match(/open\s+:\s+http:\/\/127\.0\.0\.1:\d+\/\?t=(\S+)/); if (m) token = m[1]; });
    app.stderr.on("data", (b) => { log += b.toString(); });
    await waitFor(() => token, 90000, "the app's token");
    ok("engine started with the page origin allowed", pageOrigin);
    if (/cors allows : .*reel-grab|github\.io/.test(log)) {
      const line = (log.match(/cors allows : (.*)/) || [])[1] || pageOrigin;
      check(line.indexOf(pageOrigin.replace(/^https?:\/\//, "")) >= 0 || line.indexOf(pageOrigin) >= 0,
        "allowlist printed by the app includes the page origin", line);
    }

    const child = spawn(chrome, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
      "--disable-extensions", "--hide-scrollbars", "--force-device-scale-factor=1", "--window-size=1200,1500",
      "--user-data-dir=" + profile, "--remote-debugging-port=" + cdpPort, pageUrl], { stdio: "ignore" });
    procs.push(child);
    await waitFor(async () => (await fetch(`http://127.0.0.1:${cdpPort}/json/version`).catch(() => null))?.ok, 25000, "the debugging port");
    const t = await waitFor(async () => (await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json()).find((x) => x.type === "page" && x.webSocketDebuggerUrl), 15000, "a page target");
    const ws = new WebSocket(t.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.addEventListener("open", res, { once: true }); ws.addEventListener("error", () => rej(new Error("no CDP socket")), { once: true }); });
    const cdp = new CDP(ws);
    session = { close: async () => { try { ws.close(); } catch (e) {} } };
    const errors = [];
    cdp.on((m) => {
      if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
      if (m.method === "Log.entryAdded" && m.params.entry.level === "error") errors.push(m.params.entry.text);
    });
    await cdp.send("Runtime.enable");
    await cdp.send("Log.enable");
    await cdp.send("Page.enable");
    await waitFor(() => cdp.eval("document.readyState === 'complete'"), 20000, "page load");

    /* the published page must say what it is and offer the connect path */
    await waitFor(() => cdp.eval("!!document.getElementById('connectCard')"), 15000, "the page to boot");
    const initial = JSON.parse(await cdp.eval(`JSON.stringify({
      title: document.title,
      connectVisible: !document.getElementById('connectCard').classList.contains('hidden'),
      pill: document.getElementById('eng').textContent.trim(),
      base: window.RG_BASE || null
    })`));
    check(initial.title === "reel-grab", "published page title", initial.title);
    check(initial.connectVisible, "connect card shown when there is no engine yet", initial.pill);
    check(initial.base === "http://127.0.0.1:8730", "page defaults to the local engine", String(initial.base));

    /* connect through the UI — this is where CORS/preflight/allowlist must all hold */
    await cdp.eval(`(()=>{document.getElementById('engUrl').value='http://127.0.0.1:${appPort}';
      document.getElementById('engTok').value=${JSON.stringify(token)};
      document.getElementById('connectBtn').click();return true})()`);
    let connected = true;
    try {
      await waitFor(() => cdp.eval("document.getElementById('connectCard').classList.contains('hidden')"), 30000, "the connect card to close");
    } catch (e) {
      connected = false;
      const st = await cdp.eval("document.getElementById('connectErr').textContent.trim()");
      bad("cross-origin connect succeeds (CORS + preflight)", `${e.message} | page said: ${st}`);
    }
    if (connected) ok("cross-origin connect succeeds (CORS + preflight)");
    let head = {};
    try {
      await waitFor(() => cdp.eval("document.getElementById('eng').textContent.trim() === 'engine ready'"), 180000, "engine ready via CORS");
      head = JSON.parse(await cdp.eval(`JSON.stringify({
        pill: document.getElementById('eng').textContent.trim(),
        pot: document.getElementById('pot').textContent.trim(),
        okSteps: document.querySelectorAll('#steps li.ok').length,
        outdir: document.getElementById('outdir').value })`));
    } catch (e) {
      const pill = await cdp.eval("document.getElementById('eng').textContent.trim()");
      bad("engine state readable from the published page", `${e.message} | pill: ${pill}`);
    }
    if (head.pill) {
      check(head.pill === "engine ready", "engine pill ready through the remote page", head.pill);
      check(head.okSteps === 8, "all 8 engine steps visible remotely", head.okSteps + "/8");
      check(head.pot === "token server up", "token provider visible remotely", head.pot);
    }

    /* real fetch + real download, driven entirely through the published page */
    await cdp.eval(`(()=>{document.getElementById('url').value=${JSON.stringify(CC_VIDEO)};document.getElementById('fetchBtn').click();return true})()`);
    let probeTitle = "";
    try {
      await waitFor(() => cdp.eval("!document.getElementById('probeCard').classList.contains('hidden')"), 120000, "the probe card");
      const p = JSON.parse(await cdp.eval(`JSON.stringify({title:document.getElementById('ptitle').textContent.trim(),
        meta:document.getElementById('pmeta').textContent.trim(),
        quals:document.getElementById('qual').options.length})`));
      probeTitle = p.title;
      check(p.title.startsWith("Big Buck Bunny"), "remote Fetch renders the video", p.title);
      check(p.quals > 3, "quality list populated remotely", p.quals + " options");
      check(!/NO VIDEO FORMATS/.test(p.meta), "real formats reported remotely", p.meta);
    } catch (e) {
      bad("remote Fetch renders the video", e.message);
    }

    if (probeTitle) {
      await cdp.eval(`(()=>{document.getElementById('outdir').value=${JSON.stringify(OUT)};
        document.getElementById('qual').value='360';
        document.getElementById('mode').value='video';document.getElementById('mode').onchange();
        return true})()`);
      await cdp.eval(`document.getElementById('dlBtn').onclick()`, true);
      try {
        await waitFor(() => cdp.eval("(()=>{const j=document.querySelector('.job');return !!j && /^done/.test(j.querySelector('.st').textContent.trim());})()"),
          180000, "the remote download to finish");
        const j = JSON.parse(await cdp.eval(`(()=>{const r=document.querySelector('.job');return JSON.stringify({
          status: r.querySelector('.st').textContent.trim(),
          name: r.querySelector('.nm').textContent.trim(),
          bar: r.querySelector('.bar i').style.width,
          reveal: (r.querySelector('button')||{getAttribute:()=>null}).getAttribute('onclick')||'' })})()`));
        check(/^done/.test(j.status), "remote download reached done", j.status);
        check(j.bar === "100%", "progress bar full remotely", j.bar);
        check(/Big Buck Bunny/.test(j.name), "job row names the video remotely", j.name);
        const m = j.reveal.match(/reveal\('(.+)'\)/);
        const file = m ? m[1].replace(/\\\\/g, "\\") : "";
        check(!!file && fs.existsSync(file), "file exists on disk", file);
        if (file && fs.existsSync(file)) check(fs.statSync(file).size > 500000, "downloaded file is real", (fs.statSync(file).size / 1e6).toFixed(1) + " MB");
      } catch (e) {
        const st = await cdp.eval("(()=>{const j=document.querySelector('.job');return j?j.querySelector('.st').textContent.trim():document.getElementById('jobs').textContent.trim();})()");
        bad("remote download reached done", `${e.message} | rendered: ${st}`);
      }
    }

    const shot = path.join(ROOT, "pages-shot.png");
    const r = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    fs.writeFileSync(shot, Buffer.from(r.data, "base64"));
    ok("screenshot captured", shot);

    check(errors.length === 0, "zero page exceptions / console errors", errors.slice(0, 3).join(" | "));
  } catch (e) {
    bad("published-page gate ran to completion", e && e.message ? e.message : String(e));
  } finally {
    if (session) { try { await session.close(); } catch (e) {} }
    await cleanup();
  }

  console.log(`\n${PASS} passed, ${FAIL} failed`);
  for (const f of failures) console.log("  FAIL " + f);
  process.exit(FAIL ? 1 : 0);
}

main();
