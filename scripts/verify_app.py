#!/usr/bin/env python3
"""Verification gate for reel-grab.

Boots the real server, provisions the real engine, then exercises the API against a
real Creative-Commons YouTube video and checks the downloaded bytes with ffprobe.

    python scripts/verify_app.py
    python scripts/verify_app.py --engine-home ~/.reel-grab --keep-going

Exit code 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "reel_grab.py"
CC_VIDEO = "https://www.youtube.com/watch?v=YE7VzlLtp-4"   # Big Buck Bunny, CC-BY (Blender)

PASS, FAIL, WARN = [], [], []


def ok(name, detail=""):
    PASS.append(name)
    print(f"  \033[32mPASS\033[0m {name}" + (f"  ({detail})" if detail else ""))


def bad(name, detail=""):
    FAIL.append(f"{name}: {detail}")
    print(f"  \033[31mFAIL\033[0m {name}" + (f"  ({detail})" if detail else ""))


def warn(name, detail=""):
    WARN.append(f"{name}: {detail}")
    print(f"  \033[33mWARN\033[0m {name}" + (f"  ({detail})" if detail else ""))


def check(cond, name, detail=""):
    ok(name, detail) if cond else bad(name, detail)
    return bool(cond)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def call(url, token=None, body=None, origin=None, timeout=300):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-ReelGrab-Token"] = token
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine-home", default=str(Path.home() / ".reel-grab"))
    ap.add_argument("--keep-going", action="store_true", help="don't stop at the first hard failure")
    ap.add_argument("--provision-timeout", type=int, default=1500)
    ap.add_argument("--skip-audio", action="store_true")
    args = ap.parse_args()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"\nreel-grab verification\n  app        {APP}\n  engine     {args.engine_home}\n"
          f"  port       {port}\n")

    log = open(ROOT / "verify-server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-u", str(APP), "--port", str(port),
                             "--engine-home", args.engine_home, "--no-browser"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    token = None
    try:
        # ---- 0. the published page is generated from the same source -----------
        print("[0] pages build in sync")
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_pages.py"), "--check"],
                           capture_output=True, text=True, cwd=str(ROOT))
        check(r.returncode == 0, "docs/index.html is generated, not stale",
              ((r.stdout or "") + (r.stderr or "")).strip()[:160])

        # ---- 1. boot -------------------------------------------------------
        print("[1] server boot")
        deadline = time.time() + 60
        while time.time() < deadline and token is None:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                continue
            log.write(line)
            m = re.search(r"open\s+:\s+(\S+)\?t=(\S+)", line)
            if m:
                token = m.group(2)
            if proc.poll() is not None:
                break
        if not check(token is not None, "server prints one-time token URL"):
            print("server output:\n" + (proc.stdout.read() or ""))
            return 1
        log.flush()

        # ---- 2. auth -------------------------------------------------------
        print("[2] loopback auth")
        code, _ = call(base + "/")
        check(code == 401, "no token -> 401", f"got {code}")
        code, page = call(base + f"/?t={token}")
        check(code == 200 and "reel-grab" in str(page), "token -> UI page")
        code, _ = call(base + "/api/status")
        check(code == 401, "API without token -> 401", f"got {code}")
        code, _ = call(base + "/api/probe", token=token, body={"url": CC_VIDEO},
                       origin="https://evil.example")
        check(code == 403, "cross-origin request -> 403", f"got {code}")

        # ---- 3. provisioning ----------------------------------------------
        print(f"[3] engine provisioning (up to {args.provision_timeout}s: npm install + tsc)")
        t0 = time.time()
        st = {}
        while time.time() - t0 < args.provision_timeout:
            code, st = call(base + "/api/status", token=token, timeout=30)
            if code == 200 and st.get("ready"):
                break
            if st.get("failed"):
                break
            time.sleep(3)
        print("      " + f"{time.time() - t0:.0f}s -> " +
              json.dumps({s["key"]: s["status"] for s in st.get("steps", [])}))
        for key, s in [(s["key"], s) for s in st.get("steps", [])]:
            if s["status"] != "ok":
                warn(f"step {key}", f"{s['status']} {s['detail']}")
        hard = check(st.get("ready") is True, "engine reports ready", str(st.get("failed", "")))
        tools = st.get("tools", {})
        check(bool(tools.get("ffmpeg")), "ffmpeg located", tools.get("ffmpeg", ""))
        check(bool(tools.get("yt_dlp")), "yt-dlp installed in private env")
        check(st.get("pot", {}).get("up") is True, "PO-token provider answering /ping")
        if not hard and not args.keep_going:
            return 1

        # ---- 4. probe (the PO-token gate) ---------------------------------
        print("[4] probe a CC-BY video (this is where a static app would fail)")
        code, pr = call(base + "/api/probe", token=token, body={"url": CC_VIDEO})
        if check(code == 200 and pr.get("kind") == "video", "probe returns video info",
                 pr.get("error", "")):
            check(pr.get("title") == "Big Buck Bunny", "title parsed", str(pr.get("title")))
            check(bool(pr.get("heights")), "video formats available (PO token works)",
                  f"heights={pr.get('heights')}")
            check(any(h >= 360 for h in (pr.get("heights") or [])), "at least 360p offered")
            check((pr.get("duration") or 0) > 500, "duration parsed", str(pr.get("duration")))
            check(bool(pr.get("thumbnail")), "thumbnail url present")
        else:
            bad("probe", str(pr))
            if not args.keep_going:
                return 1

        # ---- 5. real download ---------------------------------------------
        outdir = ROOT / "verify-out"
        print(f"[5] real download -> {outdir}")
        code, job = call(base + "/api/download", token=token,
                         body={"url": CC_VIDEO, "title": "Big Buck Bunny", "mode": "video",
                               "height": 360, "container": "mp4", "outdir": str(outdir)})
        if not check(code == 200 and job.get("id"), "download job accepted", str(job)):
            if not args.keep_going:
                return 1
        jid = job.get("id")
        final, t0 = None, time.time()
        while time.time() - t0 < 600:
            _, jl = call(base + "/api/jobs", token=token, timeout=30)
            final = next((j for j in jl.get("jobs", []) if j["id"] == jid), None)
            if final and final["status"] in ("done", "error", "canceled"):
                break
            time.sleep(2)
        check(final is not None and final["status"] == "done",
              "job finished", f"status={final and final['status']} err={final and final.get('error')}")
        if final and final.get("file"):
            f = Path(final["file"])
            check(f.exists(), "reported output file exists", str(f))
            check(f.stat().st_size > 500_000, "output is a real video",
                  f"{f.stat().st_size / 1e6:.1f} MB")
            probe = shutil.which("ffprobe") or str(Path.home() / "bin" / "ffprobe.exe")
            try:
                out = subprocess.run([probe, "-v", "error", "-show_entries",
                                      "format=duration,size", "-show_entries",
                                      "stream=codec_type", "-of", "json", str(f)],
                                     capture_output=True, text=True, timeout=120).stdout
                info = json.loads(out)
                kinds = {s["codec_type"] for s in info.get("streams", [])}
                check({"video", "audio"} <= kinds, "ffprobe sees video+audio (muxed)",
                      str(sorted(kinds)))
                check(float(info["format"]["duration"]) > 500, "ffprobe duration",
                      info["format"]["duration"])
            except Exception as e:
                bad("ffprobe readback", repr(e))
        else:
            bad("job reported no output path", str(final))

        # ---- 6. audio mode -------------------------------------------------
        if not args.skip_audio:
            print("[6] audio mode (mp3)")
            code, job = call(base + "/api/download", token=token,
                             body={"url": CC_VIDEO, "title": "BBB audio", "mode": "audio",
                                   "audio_format": "mp3", "outdir": str(outdir)})
            jid = job.get("id") if code == 200 else None
            final, t0 = None, time.time()
            while jid and time.time() - t0 < 600:
                _, jl = call(base + "/api/jobs", token=token, timeout=30)
                final = next((j for j in jl.get("jobs", []) if j["id"] == jid), None)
                if final and final["status"] in ("done", "error", "canceled"):
                    break
                time.sleep(2)
            if check(final is not None and final["status"] == "done", "audio job finished",
                     f"status={final and final['status']} err={final and final.get('error')}"):
                a = Path(final["file"])
                check(a.suffix == ".mp3" and a.exists(), "mp3 written", str(a))
                check(a.stat().st_size > 100_000, "mp3 has content",
                      f"{a.stat().st_size / 1e6:.1f} MB")
        else:
            warn("audio mode", "skipped by flag")

        # ---- 7. error handling ---------------------------------------------
        print("[7] error handling")
        code, r = call(base + "/api/probe", token=token, body={"url": "not a url"})
        check(code >= 400 or r.get("error"), "bad url rejected", str(r)[:120])
        code, r = call(base + "/api/opendir", token=token,
                       body={"path": str(ROOT / "does-not-exist-xyz")})
        check(code == 404, "missing folder -> 404", f"got {code}")
        code, r = call(base + "/api/cancel", token=token, body={"id": "nope"})
        check(code == 200 and r.get("ok") is False, "cancel unknown job -> ok:false")

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
        log.close()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(WARN)} warnings")
    for f in FAIL:
        print("  FAIL " + f)
    for w in WARN:
        print("  WARN " + w)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
