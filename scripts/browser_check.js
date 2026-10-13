#!/usr/bin/env node
/*
 * browser_check.js — tier-2 gate for reel-grab: drives the real UI in a real browser
 * over the DevTools Protocol. Zero dependencies (Node's fetch + global WebSocket).
 *
 *   node scripts/browser_check.js
 *   node scripts/browser_check.js --keep        # leave the screenshot + profile
 *
 * Spawns the app, reads its one-time token, then asserts what the page actually
 * renders: engine steps, responsive overflow, a real Fetch (the PO-token path), and a
 * real Download that ends in a file on disk. Exits 0 only when all of it passes.
 *
 * The probe harness (findChrome/CDP/waitFor/probe) is lifted from the
 * browser-app-verification skill's scripts/cdp_page_check.js.
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const net = require("net");
const { spawn } = require("child_process");

const ROOT = path.resolve(__dirname, "..");
const APP = path.join(ROOT, "reel_grab.py");
const OUT = path.join(ROOT, "verify-ui-out");
const CC_VIDEO = "https://www.youtube.com/watch?v=YE7VzlLtp-4"; // Big Buck Bunny, CC-BY
const KEEP = process.argv.includes("--keep");

let PASS = 0, FAIL = 0;
const failures = [];
const ok = (name, detail = "") => { PASS++; console.log(`  \x1b[32mPASS\x1b[0m ${name}${detail ? "  (" + detail + ")" : ""}`); };
const bad = (name, detail = "") => { FAIL++; failures.push(`${name}: ${detail}`); console.log(`  \x1b[31mFAIL\x1b[0m ${name}${detail ? "  (" + detail + ")" : ""}`); };
const check = (cond, name, detail = "") => { cond ? ok(name, detail) : bad(name, detail); return !!cond; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const freePort = () => new Promise((res) => {
  const s = net.createServer();
  s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => res(p)); });
});

function findChrome() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) return process.env.CHROME_PATH;
  const home = os.homedir();
  const cands = [
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    path.join(home, "AppData/Local/Google/Chrome/Application/chrome.exe"),
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ];
  const tools = path.join(home, "AppData/Local/hermes/tools");
  if (fs.existsSync(tools)) for (const d of fs.readdirSync(tools)) {
    if (d.startsWith("chromium")) cands.push(path.join(tools, d, "chrome-win64/chrome.exe"));
  }
  return cands.find((c) => fs.existsSync(c)) || null;
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.seq = 0; this.pending = new Map(); this.listeners = [];
    ws.addEventListener("message", (ev) => {
      let m; try { m = JSON.parse(typeof ev.data === "string" ? ev.data : String(ev.data)); } catch (e) { return; }
      if (m.id && this.pending.has(m.id)) {
        const { resolve, reject } = this.pending.get(m.id); this.pending.delete(m.id);
        m.error ? reject(new Error(m.error.message)) : resolve(m.result);
      } else if (m.method) for (const l of this.listeners) l(m);
    });
  }
  on(fn) { this.listeners.push(fn); }
  send(method, params) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params: params || {} }));
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); reject(new Error("CDP timeout: " + method)); } }, 30000);
    });
  }
  async eval(expression, awaitPromise) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: !!awaitPromise, userGesture: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }
}

async function waitFor(fn, timeoutMs, label) {
  const t0 = Date.now();
  for (;;) {
    let v = false; try { v = await fn(); } catch (e) { v = false; }
    if (v) return v;
    if (Date.now() - t0 > timeoutMs) throw new Error("timed out after " + timeoutMs + "ms waiting for " + label);
    await sleep(150);
  }
}

async function main() {
  const chrome = findChrome();
  if (!chrome) { console.log("harness error: no Chrome found (set CHROME_PATH)"); process.exit(1); }

  const appPort = await freePort();
  const cdpPort = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "rg-browser-"));
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(OUT, { recursive: true });

  console.log(`\nreel-grab browser gate\n  chrome  ${chrome}\n  app     http://127.0.0.1:${appPort}\n  out     ${OUT}\n`);

  const app = spawn(process.env.PYTHON || "python", ["-u", APP, "--port", String(appPort),
    "--no-browser", "--engine-home", path.join(os.homedir(), ".reel-grab")],
    { stdio: ["ignore", "pipe", "pipe"] });

  let token = null, appLog = "";
  app.stdout.on("data", (b) => {
    appLog += b.toString();
    const m = appLog.match(/open\s+:\s+http:\/\/127\.0\.0\.1:\d+\/\?t=(\S+)/);
    if (m) token = m[1];
  });
  app.stderr.on("data", (b) => { appLog += b.toString(); });

  let child = null, session = null;
  const cleanup = async () => {
    if (session) { try { await session.close(); } catch (e) {} }
    if (child) { try { child.kill(); } catch (e) {} }
    try { app.kill(); } catch (e) {}
    await sleep(500);
    if (!KEEP) { try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {} }
  };

  try {
    await waitFor(() => token, 90000, "the app's one-time token");
    ok("app boots, prints token URL", `port ${appPort}`);

    child = spawn(chrome, ["--headless=new", "--disable-gpu", "--no-first-run",
      "--no-default-browser-check", "--disable-extensions", "--hide-scrollbars",
      "--force-device-scale-factor=1", "--window-size=1200,1500",
      "--user-data-dir=" + profile, "--remote-debugging-port=" + cdpPort,
      `http://127.0.0.1:${appPort}/?t=${token}`], { stdio: "ignore" });

    await waitFor(async () => (await fetch(`http://127.0.0.1:${cdpPort}/json/version`).catch(() => null))?.ok,
      25000, "the debugging port");
    const target = await waitFor(async () => {
      const list = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json();
      return list.find((t) => t.type === "page" && t.webSocketDebuggerUrl) || false;
    }, 15000, "a page target");

    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", () => rej(new Error("could not open the CDP socket")), { once: true });
    });
    const cdp = new CDP(ws);
    session = { cdp, close: async () => { try { ws.close(); } catch (e) {} } };

    const errors = [];
    cdp.on((m) => {
      if (m.method === "Runtime.exceptionThrown") errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
      if (m.method === "Log.entryAdded" && m.params.entry.level === "error") errors.push(m.params.entry.text);
    });
    await cdp.send("Runtime.enable");
    await cdp.send("Log.enable");
    await cdp.send("Page.enable");
    await waitFor(() => cdp.eval("document.readyState === 'complete'"), 20000, "page load");
    await waitFor(() => cdp.eval("document.querySelectorAll('#steps li').length > 0"), 15000, "the status poll to render");
    /* The app withholds every action until the engine is ready, so wait on that state
     * (not a timer) before asserting the header or touching the buttons. First run on a
     * clean machine includes npm install + tsc, hence the long deadline. */
    try {
      await waitFor(() => cdp.eval("document.getElementById('eng').textContent.trim() === 'engine ready'"),
        300000, "the engine to report ready");
      ok("engine reports ready before any action is attempted");
    } catch (e) {
      const st = await cdp.eval("JSON.stringify({pill:document.getElementById('eng').textContent.trim(),note:document.getElementById('engNote').textContent.trim()})");
      bad("engine reports ready before any action is attempted", `${e.message} | ${st}`);
    }

    /* ---- header / engine panel ---- */
    const head = JSON.parse(await cdp.eval(`JSON.stringify({
      title: document.title,
      pill: document.getElementById('eng').textContent.trim(),
      pot: document.getElementById('pot').textContent.trim(),
      steps: document.querySelectorAll('#steps li').length,
      okSteps: document.querySelectorAll('#steps li.ok').length,
      setupHidden: document.getElementById('setupCard').classList.contains('hidden'),
      fetchDisabled: document.getElementById('fetchBtn').disabled,
      outdir: document.getElementById('outdir').value
    })`));
    check(head.title === "reel-grab", "page title", head.title);
    check(head.pill === "engine ready", "engine pill reads ready", head.pill);
    check(head.pot === "token server up", "token-server pill reads up", head.pot);
    check(head.steps === 8 && head.okSteps === 8, "all 8 engine steps green", `${head.okSteps}/${head.steps}`);
    check(head.setupHidden, "setup panel collapses once ready");
    check(!head.fetchDisabled, "Fetch enabled once the engine is ready");
    check(!!head.outdir, "default save folder prefilled", head.outdir);

    /* the audio-format control must match the mode on LOAD, not only after a change */
    const modeSync = JSON.parse(await cdp.eval(`JSON.stringify({
      mode: document.getElementById('mode').value,
      afmtDisabled: document.getElementById('afmt').disabled,
      qualDisabled: document.getElementById('qual').disabled
    })`));
    check(modeSync.mode === "video" && modeSync.afmtDisabled && !modeSync.qualDisabled,
      "audio-format select dimmed in video mode at load",
      `mode=${modeSync.mode} afmtDisabled=${modeSync.afmtDisabled} qualDisabled=${modeSync.qualDisabled}`);
    await cdp.eval("(()=>{const m=document.getElementById('mode');m.value='audio';m.onchange();return true})()");
    const afterAudio = JSON.parse(await cdp.eval(`JSON.stringify({
      afmtDisabled: document.getElementById('afmt').disabled,
      qualDisabled: document.getElementById('qual').disabled })`));
    check(!afterAudio.afmtDisabled && afterAudio.qualDisabled,
      "switching to audio mode enables afmt and dims quality",
      `afmtDisabled=${afterAudio.afmtDisabled} qualDisabled=${afterAudio.qualDisabled}`);
    await cdp.eval("(()=>{const m=document.getElementById('mode');m.value='video';m.onchange();return true})()");

    /* ---- responsive: no horizontal overflow ---- */
    for (const w of [390, 1024, 1500]) {
      await cdp.send("Emulation.setDeviceMetricsOverride", { width: w, height: 900, deviceScaleFactor: 1, mobile: false });
      await sleep(350);
      const over = await cdp.eval("document.documentElement.scrollWidth - window.innerWidth");
      check(over <= 0, `no horizontal overflow at ${w}px`, `overflow ${over}px`);
    }
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1200, height: 1500, deviceScaleFactor: 1, mobile: false });

    /* ---- real Fetch through the UI (the PO-token path) ---- */
    const fetchable = await cdp.eval("!document.getElementById('fetchBtn').disabled");
    if (!check(fetchable, "Fetch button is clickable before driving it")) {
      const st = await cdp.eval("JSON.stringify({pill:document.getElementById('eng').textContent.trim(),note:document.getElementById('engNote').textContent.trim()})");
      bad("Fetch renders a probe card", "skipped: Fetch disabled | " + st);
    }
    await cdp.eval(`(()=>{document.getElementById('url').value=${JSON.stringify(CC_VIDEO)};document.getElementById('fetchBtn').click();return true})()`);
    let probe = null;
    try {
      await waitFor(() => cdp.eval("!document.getElementById('probeCard').classList.contains('hidden')"), 120000, "the probe card to appear");
      probe = JSON.parse(await cdp.eval(`JSON.stringify({
        title: document.getElementById('ptitle').textContent.trim(),
        meta: document.getElementById('pmeta').textContent.trim(),
        quals: [...document.getElementById('qual').options].map(o => o.value),
        thumb: !!document.getElementById('thumb').src && !document.getElementById('thumb').classList.contains('hidden'),
        err: document.getElementById('err').textContent.trim(),
        btn: document.getElementById('fetchBtn').textContent.trim()
      })`));
    } catch (e) {
      const st = await cdp.eval(`JSON.stringify({err:document.getElementById('err').textContent.trim(),btn:document.getElementById('fetchBtn').textContent.trim()})`);
      bad("Fetch renders a probe card", `${e.message} | page state ${st}`);
    }
    if (probe) {
      check(probe.title.startsWith("Big Buck Bunny"), "probe card shows the title", probe.title);
      check(probe.quals.length > 3, "quality list populated from the probe", probe.quals.join("/"));
      check(!/NO VIDEO FORMATS/.test(probe.meta), "probe reports real video formats", probe.meta);
      check(probe.thumb, "thumbnail rendered");
      check(probe.btn === "Fetch", "Fetch button restored after the probe", probe.btn);
      check(!probe.err, "no error text after a good probe", probe.err);
    }

    /* ---- real Download through the UI, ending in a file on disk ---- */
    if (probe) {
      /* log every request the page makes after this point: assert the POST really left */
      await cdp.eval(`(()=>{window.__req=[];const of=window.fetch.bind(window);
        window.fetch=async(...a)=>{const r=await of(...a);
          try{window.__req.push(((a[1]&&a[1].method)||'GET')+' '+(typeof a[0]==='string'?a[0]:a[0].url)+' -> '+r.status);}catch(e){}
          return r;};return true})()`);
      const urlAtClick = await cdp.eval("document.getElementById('url').value.trim()");
      check(!!urlAtClick, "URL input still populated at download time", urlAtClick);
      await cdp.eval(`(()=>{document.getElementById('outdir').value=${JSON.stringify(OUT)};
        document.getElementById('qual').value='360';
        const o=document.querySelector('#qual option[value="360"]'); if(o) o.selected=true;
        return true})()`);
      /* awaitPromise must be true or we get the Promise object back, not the outcome */
      const clickResult = await cdp.eval(`(async()=>{try{await document.getElementById('dlBtn').onclick();return 'handler resolved';}catch(e){return 'handler threw: '+(e&&e.message?e.message:String(e));}})()`, true);
      console.log("      download handler -> " + clickResult);
      let job = null;
      try {
        await waitFor(() => cdp.eval("!!document.querySelector('.job')"), 30000, "a job row to render");
        const rowText = await cdp.eval("(()=>{const j=document.querySelector('.job');return j?j.querySelector('.st').textContent.trim():'none';})()");
        console.log("      job row rendered, status -> " + rowText);
      } catch (e) {
        const jobsText = await cdp.eval("document.getElementById('jobs').textContent.trim()");
        console.log("      no job row appeared; #jobs reads: " + jobsText.slice(0, 140));
      }
      try {
        await waitFor(() => cdp.eval(`(()=>{const j=document.querySelector('.job');return !!j && /^done/.test(j.querySelector('.st').textContent.trim())})()`),
           (process.env.RG_DL_MS || 60000), "a download to reach done");
        job = JSON.parse(await cdp.eval(`(()=>{const j=document.querySelector('.job');return JSON.stringify({
          name: j.querySelector('.nm').textContent.trim(),
          status: j.querySelector('.st').textContent.trim(),
          barWidth: j.querySelector('.bar i').style.width,
          reveal: (j.querySelector('button')||{}).getAttribute ? (j.querySelector('button').getAttribute('onclick')||'') : ''
        })})()`));
      } catch (e) {
        const st = await cdp.eval(`(()=>{const j=document.querySelector('.job');return JSON.stringify({
          jobsArea: document.getElementById('jobs').textContent.trim().slice(0,160),
          err: document.getElementById('err').textContent.trim(),
          dlDisabled: document.getElementById('dlBtn').disabled,
          curType: typeof cur, curTitle: (typeof cur==='object'&&cur)?cur.title:null,
          mode: document.getElementById('mode').value,
          qual: document.getElementById('qual').value,
          outdir: document.getElementById('outdir').value,
          posts: (window.__req||[]).filter(function(r){return r.indexOf('download') >= 0;}).join(' ; '),
          requestCount: (window.__req||[]).length,
          row: j ? j.querySelector('.st').textContent.trim() : 'no .job element rendered'
        })})()`);
        bad("download reaches done in the UI", `${e.message} | rendered state: ${st}`);
      }
      if (job) {
        check(/^done/.test(job.status), "job row shows done", job.status);
        check(job.barWidth === "100%", "progress bar full", job.barWidth);
        check(/Big Buck Bunny/.test(job.name), "job row names the video", job.name);
        const m = job.reveal.match(/reveal\('(.+)'\)/);
        const file = m ? m[1].replace(/\\\\/g, "\\") : "";
        check(!!file && fs.existsSync(file), "reveal button points at a file that exists", file);
        if (file && fs.existsSync(file)) {
          check(fs.statSync(file).size > 500000, "downloaded file is real", (fs.statSync(file).size / 1e6).toFixed(1) + " MB");
        }
      }
    }

    /* ---- screenshot the end state for a human look ---- */
    const shot = path.join(ROOT, "ui-flow.png");
    const r = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    fs.writeFileSync(shot, Buffer.from(r.data, "base64"));
    ok("screenshot captured", shot);

    check(errors.length === 0, "zero page exceptions / console errors", errors.slice(0, 3).join(" | "));
  } catch (e) {
    bad("browser gate ran to completion", e && e.message ? e.message : String(e));
  } finally {
    await cleanup();
  }

  console.log(`\n${PASS} passed, ${FAIL} failed`);
  for (const f of failures) console.log("  FAIL " + f);
  process.exit(FAIL ? 1 : 0);
}

main();
