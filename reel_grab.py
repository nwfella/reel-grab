#!/usr/bin/env python3
"""
reel-grab -- a local YouTube downloader that lives in one file.

    python reel_grab.py                  # opens http://127.0.0.1:8730
    python reel_grab.py --port 9000 --no-browser
    python reel_grab.py --engine-home D:/reel-grab    # where the engine is kept

Why this is a local app and not a static page like the other projects:
  * media bytes come from *.googlevideo.com, which sends no Access-Control-Allow-Origin,
    so a browser page can never fetch them;
  * those URLs embed the extracting IP and expire;
  * YouTube withholds every video format unless the client presents a PO token.
So the engine (yt-dlp + ffmpeg + a PO-token provider) has to run on this machine. This
file serves the UI on loopback and drives that engine. Standard library only.

Engine layout (all outside this file, under --engine-home, default ~/.reel-grab):
  venv/                 yt-dlp + bgutil-ytdlp-pot-provider
  pot/                  bgutil PO-token provider (node server on 127.0.0.1:4416)
"""

from __future__ import annotations

import argparse
import atexit
import http.server
import json
import os
import queue
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0
POT_PORT = int(os.environ.get("REEL_GRAB_POT_PORT", "4416"))
POT_URL = f"http://127.0.0.1:{POT_PORT}"
POT_REPO = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"

ENGINE_HOME = Path(os.environ.get("REEL_GRAB_ENGINE", Path.home() / ".reel-grab"))
VENV_DIR = ENGINE_HOME / "venv"
POT_DIR = ENGINE_HOME / "pot"
DEFAULT_OUTDIR = Path.home() / "Downloads" / "ReelGrab"

# The published GitHub Pages build: generated from this same PAGE string by
# scripts/build_pages.py, so the remote UI and the local UI can never drift apart.
PAGES_ORIGIN = os.environ.get("REEL_GRAB_PAGES_ORIGIN", "https://nwfella.github.io")
PAGES_URL = os.environ.get("REEL_GRAB_PAGES_URL", "https://nwfella.github.io/reel-grab/")

# Extra origins allowed to drive this server over CORS (the GitHub Pages build lives on a
# different origin). Loopback origins are same-origin and never need listing. Every request
# still needs the per-run token, and the server only ever binds 127.0.0.1.
ALLOWED_ORIGINS: set[str] = set()


# --------------------------------------------------------------------------- helpers

def venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    exe = f"{name}.exe" if IS_WIN else name
    for cand in (Path.home() / "bin" / exe, Path.home() / ".local" / "bin" / exe,
                 Path("/usr/local/bin") / name, Path("/opt/homebrew/bin") / name):
        if cand.exists():
            return str(cand)
    return None


def ts() -> str:
    return time.strftime("%H:%M:%S")


# --------------------------------------------------------------------------- engine

STEP_DEFS = [
    ("uv", "Locate uv (package runner)"),
    ("venv", "Create private Python env"),
    ("ytdlp", "Install yt-dlp + PO-token plugin"),
    ("node", "Locate node (token provider runtime)"),
    ("pot_repo", "Fetch token provider source"),
    ("pot_deps", "Install token provider deps"),
    ("pot_build", "Transpile token provider"),
    ("pot_server", "Start token provider on 127.0.0.1:%d" % POT_PORT),
]


class Engine:
    """Provisions and supervises the download engine."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.steps = {k: {"key": k, "label": lbl, "status": "pending", "detail": ""}
                      for k, lbl in STEP_DEFS}
        self.log: list[str] = []
        self.running = False
        self.ready = False
        self.failed = ""
        self.pot_proc: subprocess.Popen | None = None

    # -- logging ---------------------------------------------------------
    def note(self, msg: str) -> None:
        with self.lock:
            self.log.append(f"[{ts()}] {msg}")
            del self.log[:-500]

    def step(self, key: str, status: str, detail: str = "") -> None:
        with self.lock:
            st = self.steps[key]
            st["status"] = status
            if detail:
                st["detail"] = detail

    def _run(self, cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> tuple[int, str]:
        self.note("$ " + " ".join(cmd))
        env = dict(os.environ)
        env["SSL_CERT_FILE"] = ""       # miniconda ships no cacert.pem; silences noisy failures
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, creationflags=NO_WINDOW)
        except FileNotFoundError as e:
            return 127, str(e)
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s"
        out = (p.stdout or "").strip()
        for line in out.splitlines()[-25:]:
            self.note("  " + line)
        return p.returncode, out

    # -- readiness -------------------------------------------------------
    def pot_up(self) -> bool:
        try:
            with urllib.request.urlopen(POT_URL + "/ping", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def snapshot(self) -> dict:
        with self.lock:
            ytdlp = str(venv_python())
            return {
                "ready": self.ready,
                "running": self.running,
                "failed": self.failed,
                "steps": [dict(s) for s in self.steps.values()],
                "log": self.log[-200:],
                "paths": {"engine_home": str(ENGINE_HOME), "venv": ytdlp,
                          "outdir_default": str(DEFAULT_OUTDIR)},
                "tools": {
                    "uv": find_tool("uv") or "",
                    "node": find_tool("node") or "",
                    "ffmpeg": find_tool("ffmpeg") or "",
                    "ffprobe": find_tool("ffprobe") or "",
                    "yt_dlp": ytdlp if venv_python().exists() else "",
                },
                "pot": {"port": POT_PORT, "up": self.pot_up()},
            }

    # -- provisioning ----------------------------------------------------
    def provision(self) -> None:
        with self.lock:
            if self.running:
                return
            self.running = True
            self.failed = ""
            for k, _ in STEP_DEFS:
                self.steps[k].update(status="pending", detail="")
        try:
            self._provision()
        except Exception as e:                      # never take the server down
            with self.lock:
                self.failed = f"{type(e).__name__}: {e}"
            self.note(f"!! {self.failed}")
        finally:
            with self.lock:
                self.running = False

    def _provision(self) -> None:
        self.note("provisioning engine into " + str(ENGINE_HOME))
        ENGINE_HOME.mkdir(parents=True, exist_ok=True)

        # 1. uv
        self.step("uv", "running")
        uv = find_tool("uv")
        if not uv:
            self.step("uv", "fail", "uv not found on PATH")
            with self.lock:
                self.failed = ("uv is required to install the engine. "
                               "Install it from https://docs.astral.sh/uv/ and retry.")
            return
        self.step("uv", "ok", uv)
        self.note("uv: " + uv)

        # 2. venv
        self.step("venv", "running")
        if not venv_python().exists():
            rc, _ = self._run([uv, "venv", str(VENV_DIR)], timeout=600)
            if rc != 0 or not venv_python().exists():
                self.step("venv", "fail", f"uv venv exited {rc}")
                with self.lock:
                    self.failed = "could not create the private Python env"
                return
        self.step("venv", "ok", str(VENV_DIR))

        # 3. yt-dlp + plugin
        self.step("ytdlp", "running")
        rc, out = self._run([str(venv_python()), "-m", "yt_dlp", "--version"], timeout=120)
        if rc != 0:
            rc, _ = self._run([uv, "pip", "install", "--python", str(venv_python()),
                               "yt-dlp", "bgutil-ytdlp-pot-provider"], timeout=900)
            if rc != 0:
                self.step("ytdlp", "fail", f"uv pip install exited {rc}")
                with self.lock:
                    self.failed = "could not install yt-dlp"
                return
            rc, out = self._run([str(venv_python()), "-m", "yt_dlp", "--version"], timeout=120)
        self.step("ytdlp", "ok", "yt-dlp " + out.strip().splitlines()[-1] if out.strip() else "ok")

        # 4. node
        self.step("node", "running")
        node = find_tool("node")
        if not node:
            self.step("node", "fail", "node not found on PATH")
            with self.lock:
                self.failed = ("node is required for the PO-token provider "
                               "(YouTube withholds all formats without a token).")
            return
        self.step("node", "ok", node)

        # 5. provider source
        self.step("pot_repo", "running")
        if not (POT_DIR / "server" / "package.json").exists():
            rc, _ = self._run([find_tool("git") or "git", "clone", "--quiet", "--depth", "1",
                               POT_REPO, str(POT_DIR)], timeout=900)
            if rc != 0:
                self.step("pot_repo", "fail", f"git clone exited {rc}")
                with self.lock:
                    self.failed = "could not fetch the token provider source (git + network needed)"
                return
        self.step("pot_repo", "ok", str(POT_DIR))

        server = POT_DIR / "server"
        npm = find_tool("npm") or "npm"
        npx = find_tool("npx") or "npx"

        # 6. deps
        self.step("pot_deps", "running")
        if not (server / "node_modules").exists():
            rc, _ = self._run([npm, "install", "--silent"], cwd=server, timeout=1800)
            if rc != 0:
                self.step("pot_deps", "fail", f"npm install exited {rc}")
                with self.lock:
                    self.failed = "could not install the token provider dependencies"
                return
        self.step("pot_deps", "ok")

        # 7. build
        self.step("pot_build", "running")
        if not (server / "build" / "main.js").exists():
            rc, _ = self._run([npx, "tsc"], cwd=server, timeout=900)   # no "npm run build" script
            if rc != 0 or not (server / "build" / "main.js").exists():
                self.step("pot_build", "fail", f"npx tsc exited {rc}")
                with self.lock:
                    self.failed = "could not transpile the token provider"
                return
        self.step("pot_build", "ok")

        # 8. serve
        self.step("pot_server", "running")
        if not self.pot_up():
            self.pot_proc = subprocess.Popen(
                [node, "build/main.js"], cwd=str(server),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW)
            atexit.register(self.stop_pot)
            for _ in range(60):
                if self.pot_up():
                    break
                time.sleep(1)
        if not self.pot_up():
            self.step("pot_server", "fail", "provider did not answer /ping")
            with self.lock:
                self.failed = "token provider failed to start"
            return
        self.step("pot_server", "ok", f"warm on {POT_URL}")
        with self.lock:
            self.ready = True
        self.note("engine ready")

    def stop_pot(self) -> None:
        p = self.pot_proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        self.pot_proc = None


# --------------------------------------------------------------------------- jobs

PROG_RE = re.compile(
    r"\[download\]\s+(?P<pct>\d{1,3}(?:\.\d+)?)%(?:\s+of\s+(?P<size>\S+))?"
    r"(?:\s+at\s+(?P<speed>\S+))?(?:\s+ETA\s+(?P<eta>\S+))?")
# counted for progress: one per actual stream download
DEST_RE = re.compile(r"\[download\] Destination: (?P<path>.+)")
# the file the user ends up with: merge/extract/remux output wins because it comes last
FINAL_RE = re.compile(
    r"\[(?:download|ExtractAudio|Merger|FixupM3u8|FixupM4a|VideoConvertor)\]"
    r"\s+(?:Destination:\s*|Merging formats into\s+)\"?(?P<path>.+?)\"?\s*$")
ALREADY_RE = re.compile(r"\[download\] (?P<path>.+) has already been downloaded")
FORMATS_RE = re.compile(r"Downloading (\d+) format\(s\)")
ITEM_RE = re.compile(r"Downloading item (\d+) of (\d+)")


class Job:
    def __init__(self, jid: str, url: str, opts: dict) -> None:
        self.id = jid
        self.url = url
        self.opts = opts
        self.title = opts.get("title") or url
        self.status = "queued"          # queued|running|done|error|canceled
        self.pct = 0.0
        self.speed = ""
        self.eta = ""
        self.file = ""
        self.error = ""
        self.lines: queue.Queue = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.files_total = 1
        self.files_started = 0
        self.item_idx = 1
        self.items_total = 1

    def feed(self, line: str) -> None:
        self.lines.put(line)
        m = FORMATS_RE.search(line)
        if m:
            self.files_total = max(1, int(m.group(1)))
        m = ITEM_RE.search(line)
        if m:
            self.item_idx, self.items_total = int(m.group(1)), max(1, int(m.group(2)))
        if DEST_RE.search(line):
            self.files_started += 1
        m = FINAL_RE.search(line) or ALREADY_RE.search(line)
        if m:
            self.file = m.group("path").strip().strip('"')
        m = PROG_RE.search(line)
        if m:
            cur = float(m.group("pct"))
            self.speed, self.eta = m.group("speed") or "", m.group("eta") or ""
            done = max(0, self.files_started - 1)
            frac = (done + cur / 100.0) / max(1, self.files_total)
            overall = ((self.item_idx - 1) + frac) / max(1, self.items_total)
            self.pct = round(min(100.0, overall * 100.0), 1)

    def public(self) -> dict:
        return {"id": self.id, "url": self.url, "title": self.title, "status": self.status,
                "pct": self.pct, "speed": self.speed, "eta": self.eta, "file": self.file,
                "error": self.error, "mode": self.opts.get("mode", "video"),
                "height": self.opts.get("height"), "outdir": self.opts.get("outdir", ""),
                "lines": list(self.lines.queue)[-12:]}


class Jobs:
    def __init__(self, engine: Engine) -> None:
        self.lock = threading.RLock()
        self.items: dict[str, Job] = {}
        self.order: list[str] = []
        self.engine = engine
        self.queue: list[str] = []
        self.worker: threading.Thread | None = None

    def add(self, url: str, opts: dict) -> Job:
        jid = secrets.token_hex(4)
        job = Job(jid, url, opts)
        with self.lock:
            self.items[jid] = job
            self.order.append(jid)
            self.queue.append(jid)
            if not (self.worker and self.worker.is_alive()):
                self.worker = threading.Thread(target=self._drain, daemon=True)
                self.worker.start()
        return job

    def _drain(self) -> None:
        while True:
            with self.lock:
                if not self.queue:
                    return
                jid = self.queue.pop(0)
                job = self.items[jid]
            self._run(job)

    def engine_args(self, job: Job) -> list[str]:
        o = job.opts
        outdir = Path(o.get("outdir") or DEFAULT_OUTDIR)
        outdir.mkdir(parents=True, exist_ok=True)
        args = [str(venv_python()), "-m", "yt_dlp",
                "--newline", "--no-warnings", "--windows-filenames",
                "--extractor-args", "youtube:fetch_pot=always",
                "--concurrent-fragments", "4",
                # PO-token-gated formats 403 intermittently; retry before giving up
                "--retries", "5", "--fragment-retries", "5",
                "-P", str(outdir),
                "-o", "%(title)s [%(id)s].%(ext)s"]
        ff = find_tool("ffmpeg")
        if ff:
            args += ["--ffmpeg-location", str(Path(ff).parent)]
        if not o.get("playlist", False):
            args += ["--no-playlist"]

        mode = o.get("mode", "video")
        if mode == "audio":
            # prefer m4a: a plain DASH https stream that survives the token gate far more
            # consistently than the opus/webm variants
            args += ["-x", "--audio-format", o.get("audio_format", "mp3"),
                     "--audio-quality", "0", "-f", "ba[ext=m4a]/ba[ext=webm]/ba"]
            if o.get("audio_format", "mp3") == "mp3":
                args += ["--embed-metadata"]
        else:
            h = o.get("height")
            if h and h != "best":
                sel = f"bv*[height<={h}]+ba/b[height<={h}]/b"
            else:
                sel = "bv*+ba/b"
            args += ["-f", sel]
            if o.get("container", "mp4") == "mp4":
                args += ["--merge-output-format", "mp4"]
            if o.get("subs"):
                args += ["--write-subs", "--sub-langs", "en.*", "--embed-subs"]
        if o.get("retries"):
            args += ["--retries", str(o["retries"])]
        args.append(job.url)
        return args

    def _run(self, job: Job) -> None:
        job.status = "running"
        cmd = self.engine_args(job)
        self.engine.note(f"job {job.id}: " + " ".join(cmd[:6]) + " ...")
        env = dict(os.environ)
        env["SSL_CERT_FILE"] = ""
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            job.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        bufsize=1, env=env, creationflags=NO_WINDOW)
            assert job.proc.stdout is not None
            for line in job.proc.stdout:
                job.feed(line.rstrip("\n"))
            rc = job.proc.wait()
        except Exception as e:
            job.status, job.error = "error", f"{type(e).__name__}: {e}"
            return
        if job.status == "canceled":
            return
        if rc == 0:
            job.status, job.pct = "done", 100.0
        else:
            job.status = "error"
            tail = [l for l in list(job.lines.queue) if "ERROR" in l or "error" in l]
            job.error = (tail[-1] if tail else f"yt-dlp exited {rc}")[:400]

    def cancel(self, jid: str) -> bool:
        with self.lock:
            job = self.items.get(jid)
            if not job:
                return False
            job.status = "canceled"
            if job.id in self.queue:
                self.queue.remove(job.id)
        p = job.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        return True

    def public(self) -> list[dict]:
        with self.lock:
            return [self.items[j].public() for j in reversed(self.order[-30:])]


# --------------------------------------------------------------------------- probe

def probe(url: str, engine: Engine) -> dict:
    cmd = [str(venv_python()), "-m", "yt_dlp", "-J", "--flat-playlist", "--no-warnings",
           "--extractor-args", "youtube:fetch_pot=always", url]
    env = dict(os.environ)
    env["SSL_CERT_FILE"] = ""
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=300, env=env, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "").strip().splitlines()[-1] if p.stderr else "probe failed")
    info = json.loads(p.stdout)

    if info.get("_type") in ("playlist", "multi_video"):
        entries = [e for e in (info.get("entries") or []) if e]
        return {"kind": "playlist", "title": info.get("title") or "Playlist",
                "uploader": info.get("uploader") or info.get("channel") or "",
                "count": len(entries),
                "entries": [{"title": e.get("title"), "id": e.get("id"),
                             "duration": e.get("duration")} for e in entries[:60]]}

    heights = sorted({f.get("height") for f in (info.get("formats") or [])
                      if f.get("vcodec") not in (None, "none") and f.get("height")}, reverse=True)
    progressive = any(f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")
                      for f in (info.get("formats") or []))
    return {"kind": "video", "id": info.get("id"), "title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration"), "thumbnail": info.get("thumbnail"),
            "heights": heights, "progressive": progressive,
            "has_audio": any(f.get("acodec") not in (None, "none") for f in (info.get("formats") or [])),
            "license": info.get("license")}


# --------------------------------------------------------------------------- server

PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>reel-grab</title>
<link rel="icon" href="data:,">
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#272e39;--fg:#e6edf3;--dim:#8b949e;
--acc:#3fb950;--acc2:#58a6ff;--warn:#d29922;--err:#f85149;--radius:10px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:24px 18px 60px}
header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:22px;margin:0;letter-spacing:-.02em}
h1 span{color:var(--acc)}
.sub{color:var(--dim);font-size:13px;margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
padding:16px;margin-bottom:14px}
label{display:block;font-size:12px;text-transform:uppercase;letter-spacing:.06em;
color:var(--dim);margin-bottom:6px}
input,select{width:100%;background:#0b0f14;border:1px solid var(--line);color:var(--fg);
border-radius:8px;padding:10px 12px;font:inherit;font-size:14px}
input:focus,select:focus{outline:none;border-color:var(--acc2)}
button{background:var(--acc);color:#04140a;border:0;border-radius:8px;padding:10px 16px;
font:inherit;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.1)}
button:disabled{opacity:.45;cursor:not-allowed}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line);font-weight:500}
button.sm{padding:6px 10px;font-size:12px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.row>*{flex:1 1 150px}
.row>button{flex:0 0 auto}
#outdir{flex:2 1 240px}   /* a real Windows path needs more room than a dropdown */
.pill{font-size:12px;padding:3px 9px;border-radius:99px;border:1px solid var(--line);color:var(--dim)}
.pill.ok{color:var(--acc);border-color:#1f6f37;background:#0d2616}
.pill.bad{color:var(--err);border-color:#6e2422;background:#2a1211}
.pill.busy{color:var(--warn);border-color:#6b5213;background:#2a2210}
.hidden{display:none!important}
.meta{display:grid;grid-template-columns:96px 1fr;gap:14px}
.meta img{width:96px;aspect-ratio:16/9;object-fit:cover;border-radius:8px;background:#0b0f14}
.t{font-weight:600;margin:0 0 4px;font-size:15px}
.d{color:var(--dim);font-size:13px;margin:0}
.bar{height:8px;background:#0b0f14;border:1px solid var(--line);border-radius:99px;overflow:hidden;
margin:8px 0 6px}
.bar i{display:block;height:100%;background:var(--acc);width:0;transition:width .25s ease}
.bar.err i{background:var(--err)}
.job{border-top:1px solid var(--line);padding:12px 0}
.job:first-child{border-top:0}
.job .top{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.job .nm{font-size:14px;font-weight:500;word-break:break-word}
.job .st{font-size:12px;color:var(--dim);white-space:nowrap}
pre{background:#0b0f14;border:1px solid var(--line);border-radius:8px;padding:10px;
overflow:auto;max-height:220px;font:12px/1.45 ui-monospace,Consolas,monospace;
color:var(--dim);margin:8px 0 0}
.steps{list-style:none;padding:0;margin:0 0 8px}
.steps li{display:flex;gap:10px;align-items:flex-start;padding:4px 0;font-size:13px}
.dot{width:16px;flex:0 0 16px;text-align:center;color:var(--dim)}
.steps li.ok .dot{color:var(--acc)}
.steps li.fail .dot{color:var(--err)}
.steps li.running .dot{color:var(--warn)}
.steps small{color:var(--dim);display:block;word-break:break-all}
.note{font-size:12px;color:var(--dim);margin-top:10px}
.entries{max-height:180px;overflow:auto;font-size:13px;color:var(--dim);
border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-top:10px}
.entries div{padding:2px 0}
code{background:#0b0f14;border:1px solid var(--line);border-radius:4px;padding:1px 5px;
font:12px ui-monospace,Consolas,monospace;color:var(--fg)}
@media(max-width:560px){.meta{grid-template-columns:1fr}.meta img{width:100%}}
</style></head><body><div class="wrap">

<header>
  <h1>reel<span>-grab</span></h1>
  <span id="eng" class="pill">engine?</span>
  <span id="pot" class="pill">token?</span>
  <span style="flex:1"></span>
  <button class="ghost sm" id="setupBtn">Engine setup</button>
</header>
<p class="sub">Local YouTube downloader. Runs entirely on this machine &mdash; the engine must be local
because googlevideo sends no CORS headers and YouTube gates formats behind PO tokens.</p>

<section class="card hidden" id="connectCard">
  <label>Connect to your local engine</label>
  <p class="note">This page is served from GitHub Pages and cannot download anything by
  itself &mdash; googlevideo sends no CORS headers and YouTube gates formats behind PO
  tokens, so the engine has to run on your machine. Start it with
  <code>python reel_grab.py</code>, then paste the token it printed.</p>
  <div class="row">
    <div><label for="engUrl">Engine URL</label>
      <input id="engUrl" value="http://127.0.0.1:8730" spellcheck="false"></div>
    <div><label for="engTok">Token</label>
      <input id="engTok" placeholder="from the console" spellcheck="false"></div>
    <button id="connectBtn">Connect</button>
  </div>
  <p class="note" id="connectErr" style="color:var(--err)"></p>
</section>

<section class="card" id="setupCard">
  <label>Engine</label>
  <ul class="steps" id="steps"></ul>
  <div class="row">
    <button id="provBtn">Provision engine</button>
    <button class="ghost" id="logBtn">Toggle log</button>
  </div>
  <pre id="log" class="hidden"></pre>
  <p class="note" id="engNote"></p>
</section>

<section class="card">
  <label for="url">Video or playlist URL</label>
  <div class="row">
    <input id="url" placeholder="https://www.youtube.com/watch?v=..." autocomplete="off">
    <button id="fetchBtn">Fetch</button>
  </div>
  <div id="err" class="note" style="color:var(--err)"></div>
</section>

<section class="card hidden" id="probeCard">
  <div class="meta">
    <img id="thumb" alt="">
    <div>
      <p class="t" id="ptitle"></p>
      <p class="d" id="pmeta"></p>
    </div>
  </div>
  <div class="entries hidden" id="entries"></div>
  <div class="row" style="margin-top:14px">
    <div><label for="mode">Mode</label>
      <select id="mode">
        <option value="video">Video (merged mp4)</option>
        <option value="audio">Audio only</option>
      </select></div>
    <div><label for="qual">Quality</label><select id="qual"></select></div>
    <div><label for="afmt">Audio format</label>
      <select id="afmt"><option value="mp3">mp3</option><option value="m4a">m4a</option>
      <option value="opus">opus</option></select></div>
    <div><label for="outdir">Save to</label><input id="outdir" spellcheck="false"></div>
    <button id="dlBtn">Download</button>
  </div>
  <p class="note" id="lic"></p>
</section>

<section class="card">
  <label>Downloads</label>
  <div id="jobs"><p class="note">Nothing yet.</p></div>
</section>

</div><script>
const q=new URLSearchParams(location.search);
/* Standalone (GitHub Pages) build: the page is not served by the engine, so it must be
   told where the engine lives and must carry the token the app printed. Everything else
   is the same UI, generated from the same source. */
const STANDALONE=!!window.RG_STANDALONE;
let BASE=window.RG_BASE||'';
let TOKEN=q.get('t')||(location.hash.match(/t=([^&]+)/)||[])[1]||'';
if(STANDALONE){
  BASE=(localStorage.getItem('rg_engine')||window.RG_BASE||'http://127.0.0.1:8730').replace(/\/+$/,'');
  TOKEN=TOKEN||localStorage.getItem('rg_token')||'';
}
const api=async(p,b)=>{const r=await fetch(BASE+p,{method:b?'POST':'GET',
  headers:{'Content-Type':'application/json','X-ReelGrab-Token':TOKEN},
  body:b?JSON.stringify(b):undefined});
  const j=await r.json().catch(()=>({error:'bad response '+r.status}));
  if(!r.ok||j.error)throw new Error(j.error||('HTTP '+r.status));return j;};
const $=id=>document.getElementById(id);
let cur=null,busy=false;

function fmtDur(s){if(!s)return'?';s=Math.round(s);const h=Math.floor(s/3600),
  m=Math.floor(s%3600/60),x=s%60;return (h?h+':':'')+String(m).padStart(h?2:1,'0')+':'
  +String(x).padStart(2,'0');}

function renderStatus(st){
  const e=$('eng'),p=$('pot');
  e.className='pill '+(st.ready?'ok':(st.running?'busy':'bad'));
  e.textContent=st.ready?'engine ready':(st.running?'provisioning':'engine not ready');
  p.className='pill '+(st.pot.up?'ok':'bad');
  p.textContent=st.pot.up?'token server up':'token server down';
  $('steps').innerHTML=st.steps.map(s=>{
    const i={ok:'\u2713',fail:'\u2717',running:'\u25cf',pending:'\u25cb'}[s.status]||'\u25cb';
    return `<li class="${s.status}"><span class="dot">${i}</span><span>${s.label}`+
      (s.detail?`<small>${s.detail}</small>`:'')+'</span></li>';}).join('');
  if(st.log.length)$('log').textContent=st.log.join('\n');
  const t=st.tools||{};
  $('engNote').textContent=st.failed?('Problem: '+st.failed)
    :(st.ready?`uv: ${t.uv?'found':'missing'} \u00b7 node: ${t.node?'found':'missing'} \u00b7 ffmpeg: ${t.ffmpeg?'found':'missing'} \u00b7 yt-dlp: ${t.yt_dlp||'not installed'}`
    :'Provision the engine to download anything.');
  if(st.paths&&!$('outdir').value)$('outdir').value=st.paths.outdir_default;
  $('provBtn').disabled=!!st.running;
  $('provBtn').textContent=st.ready?'Re-check / update':'Provision engine';
  $('fetchBtn').disabled=!st.ready||busy;
  $('setupCard').classList.toggle('hidden',st.ready&&!showSetup);
  $('log').classList.toggle('hidden',!showLog);
  return st;
}
let showLog=false,showSetup=false,connected=false;
function showConnect(msg){$('connectCard').classList.remove('hidden');
  $('connectErr').textContent=msg||'';const e=$('eng');
  e.className='pill bad';e.textContent='local engine not reachable';
  $('pot').className='pill';$('pot').textContent='token?';}
if(STANDALONE){$('engUrl').value=BASE;$('engTok').value=TOKEN;if(!TOKEN)showConnect('');}
$('connectBtn').onclick=async()=>{
  BASE=$('engUrl').value.trim().replace(/\/+$/,'');TOKEN=$('engTok').value.trim();
  try{localStorage.setItem('rg_engine',BASE);localStorage.setItem('rg_token',TOKEN);}catch(e){}
  $('connectErr').textContent='connecting\u2026';
  try{await api('/api/status');connected=true;$('connectCard').classList.add('hidden');poll();}
  catch(e){showConnect(e.message);}};
async function poll(){try{const st=renderStatus(await api('/api/status'));
  connected=true;$('connectCard').classList.add('hidden');
  /* /api/jobs returns {jobs:[...]}: passing the wrapper made js.length undefined,
     which is falsy, so the list rendered its empty state forever. */
  const jobs=(await api('/api/jobs')).jobs||[];renderJobs(jobs);
  const active=jobs.some(j=>j.status==='running'||j.status==='queued');
  setTimeout(poll,st.running||active?700:2500);}catch(e){
  if(STANDALONE){showConnect(e.message);return;}   /* don't hammer a dead engine; Connect resumes */
  setTimeout(poll,3000);}}
$('logBtn').onclick=()=>{showLog=!showLog;$('log').classList.toggle('hidden',!showLog);};
$('provBtn').onclick=async()=>{try{await api('/api/setup',{});poll();}
  catch(e){$('err').textContent=e.message;}};
$('setupBtn').onclick=()=>{showSetup=!showSetup;
  $('setupCard').classList.toggle('hidden',!showSetup);
  $('setupCard').scrollIntoView({behavior:'smooth'});};

$('fetchBtn').onclick=doFetch;
$('url').addEventListener('keydown',e=>{if(e.key==='Enter')doFetch();});
async function doFetch(){
  const url=$('url').value.trim();if(!url)return;
  busy=true;$('fetchBtn').disabled=true;$('fetchBtn').textContent='Fetching\u2026';
  $('err').textContent='';$('probeCard').classList.add('hidden');
  try{cur=await api('/api/probe',{url});renderProbe(cur);}
  catch(e){$('err').textContent=e.message;}
  busy=false;$('fetchBtn').textContent='Fetch';
}
function renderProbe(pr){
  $('probeCard').classList.remove('hidden');
  $('ptitle').textContent=pr.title||'(untitled)';
  const bits=[];
  if(pr.kind==='playlist')bits.push(pr.count+' videos');
  else{bits.push(fmtDur(pr.duration));if(pr.uploader)bits.push(pr.uploader);
       if(pr.heights&&pr.heights.length)bits.push('max '+pr.heights[0]+'p');
       else bits.push('NO VIDEO FORMATS \u2014 engine problem');}
  $('pmeta').textContent=bits.join(' \u00b7 ');
  const th=$('thumb');th.src=pr.thumbnail||'';th.classList.toggle('hidden',!pr.thumbnail);
  $('lic').textContent=pr.license?('License reported by YouTube: '+pr.license):'';
  const q=$('qual');q.innerHTML='';
  const hs=(pr.heights&&pr.heights.length)?pr.heights:[1080,720,480,360];
  q.innerHTML='<option value="best">best available</option>'+
    hs.map(h=>`<option value="${h}"${h===720?' selected':''}>${h}p</option>`).join('');
  const en=$('entries');
  if(pr.kind==='playlist'&&pr.entries&&pr.entries.length){
    en.classList.remove('hidden');
    en.innerHTML=pr.entries.map(e=>`<div>${e.title||e.id}${e.duration?' \u00b7 '+fmtDur(e.duration):''}</div>`).join('');
  }else en.classList.add('hidden');
}
$('dlBtn').onclick=async()=>{
  const url=$('url').value.trim();
  if(!url){$('err').textContent='Paste a video or playlist URL first.';return;}
  /* never derive everything from the probe: yt-dlp does not need it, and a primary
     button that silently does nothing is a defect, not a guard. */
  const body={url,title:(cur&&cur.title)||url,playlist:!!(cur&&cur.kind==='playlist'),
    mode:$('mode').value,height:$('qual').value,audio_format:$('afmt').value,
    outdir:$('outdir').value.trim()};
  $('dlBtn').disabled=true;
  try{await api('/api/download',body);$('err').textContent='';poll();}
  catch(e){$('err').textContent=e.message;}
  finally{$('dlBtn').disabled=false;}
};
function renderJobs(js){
  const w=$('jobs');
  if(!js.length){w.innerHTML='<p class="note">Nothing yet.</p>';return;}
  w.innerHTML=js.map(j=>{
    const cls=Math.min(100,j.pct||0);
    const bad=j.status==='error'||j.status==='canceled';
    const st=j.status==='done'?'done':(j.status==='running'
      ?`${cls.toFixed(0)}% ${j.speed||''} ${j.eta?'ETA '+j.eta:''}`:j.status);
    return `<div class="job"><div class="top"><span class="nm">${j.title||j.url}</span>
      <span class="st">${st}</span></div>
      <div class="bar ${bad?'err':''}"><i style="width:${bad?(j.status==='error'?100:cls):cls}%"></i></div>
      ${j.error?`<p class="note" style="color:var(--err)">${j.error}</p>`:''}
      <div style="display:flex;gap:8px;margin-top:6px">
      ${j.file?`<button class="ghost sm" onclick="reveal('${j.file.replace(/\\/g,'\\\\').replace(/'/g,"\\'")}')">Open folder</button>`:''}
      ${j.status==='running'||j.status==='queued'
        ?`<button class="ghost sm" onclick="cancel('${j.id}')">Cancel</button>`:''}
      </div></div>`;}).join('');
}
async function cancel(id){await api('/api/cancel',{id});poll();}
async function reveal(path){await api('/api/opendir',{path});}
function syncMode(){const a=$('mode').value==='audio';
  $('afmt').disabled=!a;$('qual').disabled=a;}
$('mode').onchange=syncMode;
syncMode();   /* run it at init too: the control must match the mode on load, not only after a change */
/* The published page must not probe a default engine URL before it has a token: that is a
   guaranteed console error on every fresh visit, and it hammers a port it knows nothing
   about. Wait for the user to press Connect instead. */
if(!(STANDALONE&&!TOKEN)){poll();}
</script></body></html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "reel-grab"
    engine: Engine
    jobs: Jobs
    token: str = ""

    def log_message(self, *a):        # keep the console clean
        pass

    # -- helpers ---------------------------------------------------------
    def _cors(self) -> dict:
        """Headers that let a listed origin (e.g. the GitHub Pages build) read the reply.
        Chrome's Private Network Access also needs the loopback opt-in on the preflight."""
        o = self.headers.get("Origin")
        if o and o in ALLOWED_ORIGINS:
            return {"Access-Control-Allow-Origin": o,
                    "Access-Control-Allow-Headers": "Content-Type, X-ReelGrab-Token",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Max-Age": "600",
                    "Access-Control-Allow-Private-Network": "true",
                    "Vary": "Origin"}
        return {}

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in self._cors().items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        """Loopback-only + token + same-origin: stops a random web page from
        driving this server (any site can POST to 127.0.0.1)."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            self._json({"error": "bad host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            same = any(origin == f"http://{h}:{self.server.server_address[1]}"
                       for h in ("127.0.0.1", "localhost", "[::1]"))
            if not same and origin not in ALLOWED_ORIGINS:
                self._json({"error": "cross-origin blocked"}, 403)
                return False
        tok = self.headers.get("X-ReelGrab-Token")
        if tok != self.token:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("t", [""])[0] != self.token:
                self._json({"error": "missing or bad token"}, 401)
                return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- routes ----------------------------------------------------------
    def do_OPTIONS(self):
        """CORS preflight. Only listed origins get an answer, and the loopback opt-in is
        what Chrome requires before an HTTPS page may call a local address."""
        o = self.headers.get("Origin") or ""
        if o not in ALLOWED_ORIGINS:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        for k, v in self._cors().items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("t", [""])[0] != self.token:
                self._json({"error": "missing or bad token"}, 401)
                return
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in self._cors().items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._auth():
            return
        if path == "/api/status":
            self._json(self.engine.snapshot())
        elif path == "/api/jobs":
            self._json({"jobs": self.jobs.public()})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._auth():
            return
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/setup":
                if not self.engine.running:
                    threading.Thread(target=self.engine.provision, daemon=True).start()
                self._json({"started": True})
            elif path == "/api/probe":
                url = (body.get("url") or "").strip()
                if not url:
                    return self._json({"error": "url required"}, 400)
                if not self.engine.ready and not venv_python().exists():
                    return self._json({"error": "engine not provisioned yet"}, 409)
                self._json(probe(url, self.engine))
            elif path == "/api/download":
                url = (body.get("url") or "").strip()
                if not url:
                    return self._json({"error": "url required"}, 400)
                if not self.engine.ready:
                    return self._json({"error": "engine not ready"}, 409)
                job = self.jobs.add(url, body)
                self._json({"id": job.id})
            elif path == "/api/cancel":
                self._json({"ok": self.jobs.cancel(body.get("id", ""))})
            elif path == "/api/opendir":
                p = Path(body.get("path") or "")
                if p.is_file():
                    d = p.parent
                elif p.is_dir():
                    d = p
                else:
                    return self._json({"error": "path not found"}, 404)
                if IS_WIN:
                    os.startfile(str(d))                       # noqa: S606
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(d)])
                else:
                    subprocess.Popen(["xdg-open", str(d)])
                self._json({"ok": True, "dir": str(d)})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    global ENGINE_HOME, VENV_DIR, POT_DIR
    ap = argparse.ArgumentParser(description="reel-grab -- local YouTube downloader")
    ap.add_argument("--port", type=int, default=8730)
    ap.add_argument("--engine-home", default=str(ENGINE_HOME))
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-provision", action="store_true",
                    help="start the UI without provisioning the engine")
    ap.add_argument("--allow-origin", action="append", default=None, metavar="ORIGIN",
                    help="extra origin allowed to drive this server over CORS, e.g. the "
                         "GitHub Pages build. Repeatable. Pass 'none' to disable. "
                         f"Default: {PAGES_ORIGIN}")
    args = ap.parse_args()

    ALLOWED_ORIGINS.clear()
    for o in (args.allow_origin if args.allow_origin is not None else [PAGES_ORIGIN]):
        if o and o.strip().lower() != "none":
            ALLOWED_ORIGINS.add(o.strip().rstrip("/"))

    ENGINE_HOME = Path(args.engine_home).expanduser().resolve()
    VENV_DIR = ENGINE_HOME / "venv"
    POT_DIR = ENGINE_HOME / "pot"

    engine = Engine()
    jobs = Jobs(engine)
    Handler.engine, Handler.jobs = engine, jobs
    Handler.token = secrets.token_urlsafe(18)

    httpd = Server(("127.0.0.1", args.port), Handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={Handler.token}"

    print("reel-grab")
    print("  engine home : " + str(ENGINE_HOME))
    print("  output      : " + str(DEFAULT_OUTDIR))
    print("  open        : " + url)
    print("  github pages: " + PAGES_URL + "#t=" + Handler.token)
    print("  cors allows : " + (", ".join(sorted(ALLOWED_ORIGINS)) or "(no extra origins)"))
    print("  (keep this window open; Ctrl+C to stop)", flush=True)

    if not args.no_provision:
        threading.Thread(target=engine.provision, daemon=True).start()
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        engine.stop_pot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
