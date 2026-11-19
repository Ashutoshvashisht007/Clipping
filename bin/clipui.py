#!/usr/bin/env python3
"""Clipping page: a local web page (http://localhost:8765) for finding moments and making clips.

Start it with bin/clipui, or double-click Clipping.command in the Clipping folder.
It listens only on this Mac. The work itself is done by clipkit.py, run as a separate
process for every job exactly like the terminal commands, so those keep working on their own.
"""
from __future__ import annotations

import argparse
import codecs
import csv
import datetime as dt
import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import clipkit as ck  # noqa: E402  (the engine: same folder)
import yaml  # noqa: E402

ROOT = ck.ROOT
UI_DIR = ROOT / "ui"
CLIPKIT = str(BIN / "clipkit.py")
TOKEN = secrets.token_urlsafe(24)
PORT = 8765
JOB_DIR = ROOT / "inbox" / "ui-jobs"
FRAME_DIR = ROOT / "inbox" / "ui-frames"
SERVE_DIRS = ("out", "layouts", "inbox/ui-frames", "brand", "inbox/live/previews")
SOURCES = ("none", "whop_campaign", "written", "public_statement")
LOG_EDITABLE = ("title", "youtube_url", "instagram_url", "views_24h", "views_7d", "avg_pct_watched",
                "viewed_vs_swiped", "followers_gained", "shares", "notes")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
         ".mp4": "video/mp4", ".json": "application/json", ".txt": "text/plain; charset=utf-8"}


class BadRequest(Exception):
    pass


class NotFound(Exception):
    pass


# ───────────────────────────── jobs ─────────────────────────────

class Job:
    def __init__(self, kind: str, label: str, args: list[str], lane: str | None = None, meta: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.label, self.args, self.lane = kind, label, args, lane
        self.meta = meta or {}
        self.status = "queued"
        self.lines: list[str] = []
        self.result: dict | None = None
        self.error = ""
        self.created = time.time()
        self.started: float | None = None
        self.ended: float | None = None
        self.proc: subprocess.Popen | None = None
        self.stop_requested = False
        self.result_path: Path | None = None
        self.lock = threading.Lock()

    def add(self, text: str, replace: bool = False) -> None:
        text = ANSI.sub("", text).rstrip()
        with self.lock:
            if replace and self.lines:
                if text:
                    self.lines[-1] = text
            elif text:
                self.lines.append(text)
                del self.lines[:-400]

    def view(self) -> dict:
        with self.lock:
            lines = self.lines[-30:]
        if self.status == "running" and self.kind == "live" and self.result_path:  # live mode reports as it goes
            try:
                self.result = json.loads(self.result_path.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        res = dict(self.result) if self.result else None
        if res and str(res.get("file", "")).endswith(".mp4") and (ROOT / res["file"]).exists():
            res["url"] = file_url(ROOT / res["file"])
        if res and isinstance(res.get("meta"), dict):
            res["meta"] = public_meta(res["meta"])
        if res and res.get("frame"):
            res["frame_rel"] = rel(Path(res["frame"]))
            res["frame_url"] = file_url(Path(res["frame"]))
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status, "meta": self.meta,
                "lines": lines, "error": self.error, "result": res, "created": self.created,
                "started": self.started, "ended": self.ended}


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
# one at a time per lane, so the Mac isn't swamped; context has its own so it never waits behind a clip
LANES = {"find": queue.Queue(), "make": queue.Queue(), "context": queue.Queue()}


def pump(job: Job) -> None:
    """Copy the command's output into the job, treating \\r as 'replace the last line' (progress bars)."""
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    buf, replace = "", False
    while True:
        chunk = job.proc.stdout.read1(65536)
        if not chunk:
            break
        buf += dec.decode(chunk)
        while m := re.search(r"\r\n|\n|\r", buf):
            job.add(buf[:m.start()], replace)
            replace = m.group() == "\r"
            buf = buf[m.end():]
    buf += dec.decode(b"", final=True)
    if buf:
        job.add(buf, replace)


def error_text(lines: list[str]) -> str:
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("✗"):
            return "\n".join([lines[i][1:].strip()] + lines[i + 1:]).strip()
    return "\n".join(lines[-4:]) or "It stopped without saying why."


def run_job(job: Job) -> None:
    if job.stop_requested:
        job.status, job.ended = "stopped", time.time()
        return
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    result_path = JOB_DIR / f"{job.id}.json"
    job.result_path = result_path
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "CLIPKIT_RESULT": str(result_path), "CLIPKIT_ROOT": str(ROOT)}
    job.status, job.started = "running", time.time()
    rc = 1
    try:
        job.proc = subprocess.Popen([sys.executable, "-I", CLIPKIT, *job.args], cwd=str(ROOT), env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        pump(job)
        rc = job.proc.wait()
    except OSError as e:
        job.add(f"✗ Couldn't start it: {e}")
    if result_path.exists():
        try:
            job.result = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        result_path.unlink(missing_ok=True)
    if job.stop_requested:
        job.status = "stopped"
    elif rc == 0:
        job.status = "done"
    else:
        job.status = "failed"
        job.error = error_text(job.lines)
    job.ended = time.time()


def lane_worker(q: queue.Queue) -> None:
    while True:
        job = q.get()
        try:
            run_job(job)
        except Exception as e:  # never let one bad job stop the lane
            job.status, job.error, job.ended = "failed", f"Unexpected error: {e}", time.time()


def submit(job: Job) -> Job:
    with JOBS_LOCK:
        JOBS[job.id] = job
        finished = [j.id for j in JOBS.values() if j.status not in ("queued", "running")]
        for jid in finished[: max(0, len(JOBS) - 60)]:
            JOBS.pop(jid, None)
    if job.lane:
        LANES[job.lane].put(job)
    else:
        threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return job


def stop_job(job: Job) -> None:
    job.stop_requested = True
    if job.status == "queued":
        job.status, job.ended = "stopped", time.time()
    p = job.proc
    if p and p.poll() is None:
        try:  # SIGINT lets a recording close its file cleanly
            os.killpg(p.pid, signal.SIGINT if job.kind in ("record", "live") else signal.SIGTERM)
        except ProcessLookupError:
            pass

        def hard_stop():
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        t = threading.Timer(10, hard_stop)
        t.daemon = True
        t.start()


def active(kind: str, **meta) -> Job | None:
    with JOBS_LOCK:
        for j in JOBS.values():
            if j.kind == kind and j.status in ("queued", "running") and all(j.meta.get(k) == v for k, v in meta.items()):
                return j
    return None


# ───────────────────────────── files + data ─────────────────────────────

def rel(p: Path) -> str:
    return str(Path(p).resolve().relative_to(ROOT))


def file_url(p: Path) -> str:
    p = Path(p)
    try:
        v = int(p.stat().st_mtime)
    except OSError:
        v = 0
    return f"/files/{rel(p)}?v={v}"


def safe_path(relpath: str, allowed=SERVE_DIRS) -> Path:
    p = (ROOT / unquote(str(relpath)).lstrip("/")).resolve()
    for d in allowed:
        base = (ROOT / d).resolve()
        if p == base or p.is_relative_to(base):
            return p
    raise NotFound("That file isn't part of the Clipping folder.")


def need_str(body: dict, key: str, limit: int = 2000, required: bool = True) -> str:
    v = body.get(key)
    v = "" if v is None else str(v).strip()
    if required and not v:
        raise BadRequest(f"Missing {key}.")
    if len(v) > limit:
        raise BadRequest(f"{key} is too long.")
    return v


def source(link: str) -> dict:
    if link.startswith("-"):
        raise BadRequest("That doesn't look like a link.")
    try:
        return ck.source_of(link)
    except ck.ClipError as e:
        raise BadRequest(str(e))


def source_arg(src: dict) -> str:
    return src["path"] if src["kind"] == "file" else src["url"]


def time_arg(v, what: str) -> float:
    try:
        t = ck.parse_time(v) if isinstance(v, str) else float(v)
    except (ValueError, TypeError):
        raise BadRequest(f"Can't read the {what} time '{v}'. Use 1:23:45, 83:45 or 5025.")
    if t < 0:
        raise BadRequest(f"The {what} time can't be negative.")
    return t


def status_view() -> dict:
    cfg = ck.load_config()
    return {
        "root": str(ROOT), "ffmpeg": Path(ck.FFMPEG).exists(),
        "model": (ck.model_path(cfg) / "model.safetensors").exists(),
        "ai": ck.ai_status(cfg), "ai_text": bool(cfg["clips"].get("ai_text", True)),
        "disk_free_gb": round(shutil.disk_usage(Path.home()).free / 1e9),
        "hashtags": cfg["clips"].get("hashtags") or [], "checklist": ck.CHECKLIST,
        "cut_silence": bool(cfg["clips"].get("cut_silence", True)),
    }


def streamers_view() -> list[dict]:
    st = ck.load_state()
    streams = list(st["streams"].values())
    out = []
    for name, s in ck.load_streamers().items():
        ok, src = ck.permission(s)
        p = s.get("permission") or {}
        lay = ck.load_layout(name)
        prev = ROOT / "layouts" / f"{name}-preview.png"
        last = next((v.get("url", "") for v in reversed(streams) if v.get("streamer") == name), "")
        set_from = str(lay.get("set_from", ""))
        link, sep, at = set_from.rpartition(" at ")
        if not sep or not ck.is_time(at):
            link, at = "", ""
        out.append({
            "name": name, "display": s.get("display", name), "channel": s.get("channel", ""),
            "youtube": s.get("youtube", ""), "credit": s.get("credit", ""), "notes": s.get("notes", ""),
            "permission": {"ok": ok, "source": src if src in SOURCES else "none", "proof": str(p.get("proof") or ""),
                           "checked_on": str(p.get("checked_on") or ""), "monetize": bool(p.get("monetize"))},
            "layout": {"mode": lay.get("mode", "full"), "cam": lay.get("cam"),
                       "set_link": link if link and link != "None" else last, "set_time": at if at != "None" else "",
                       "preview": file_url(prev) if prev.exists() and lay.get("mode") == "camera_top" else ""},
            "last_stream": last,
        })
    return out


def moments_path(v: dict) -> Path | None:
    mf = Path(v.get("moments_file") or "")
    if mf.name and mf.exists():
        return mf
    alt = ROOT / "moments" / str(v.get("streamer", "")) / mf.name
    return alt if mf.name and alt.exists() else None


def streams_view() -> list[dict]:
    st = ck.load_state()
    rows = []
    for key, v in st["streams"].items():
        mf = moments_path(v)
        rows.append({"key": key, "streamer": v.get("streamer", ""), "title": v.get("title", ""), "live": bool(v.get("live")),
                     "date": v.get("date", ""), "url": v.get("url", ""), "count": len(v.get("moments") or []),
                     "full": bool(mf and mf.with_suffix(".json").exists()),
                     "t": mf.stat().st_mtime if mf else 0})
    rows.sort(key=lambda r: -r["t"])
    return rows


EDIT_KEYS = ("source_url", "start", "end")


def public_meta(meta: dict) -> dict:
    """A clip's details for the page: the post texts filled in, the bulky caption words left out."""
    m = {k: v for k, v in meta.items() if k not in ("words", "heard", "keep")}
    m["yt_description"], m["ig_caption"] = ck.post_fields(meta)
    return m


def clip_meta(mp4: Path) -> dict:
    js = mp4.with_suffix(".json")
    if js.exists():
        try:
            return json.loads(js.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    return meta_from_txt(mp4)


def meta_from_txt(mp4: Path) -> dict:
    """Clips made before the page existed have only a .txt: read what we can from it."""
    txt = mp4.with_suffix(".txt")
    lines = txt.read_text().splitlines() if txt.exists() else []
    meta = {"version": 0, "file": rel(mp4), "title": "", "caption": "", "permission": "", "hook": "",
            "practice": mp4.name.startswith("PRACTICE-")}
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("TITLE") and i + 1 < len(lines):
            t = lines[i + 1].strip()
            meta["title"] = "" if t.startswith("<") else t
            i += 2
            continue
        if ln.startswith("CREDIT + HASHTAGS"):
            j, block = i + 1, []
            while j < len(lines) and lines[j].strip():
                block.append(lines[j])
                j += 1
            meta["caption"] = "\n".join(block)
            i = j
            continue
        if ln.startswith("PERMISSION:"):
            meta["permission"] = ln
        if "already burned in" in ln:
            meta["hook"] = "yes"
        i += 1
    stem = mp4.stem.removeprefix("PRACTICE-")
    for name, s in ck.load_streamers().items():
        if stem.startswith(name + "-"):
            meta["streamer"], meta["display"] = name, s.get("display", name)
            tag = stem[len(name) + 1:]
            if re.fullmatch(r"\d+(-\d\d){1,2}", tag):
                meta["start"] = ck.parse_time(tag.replace("-", ":"))
            break
    m = re.search(r"Full stream: (\S+)", meta["caption"])
    if m:
        try:
            meta["source_key"] = ck.source_of(m[1])["key"]
            meta["stream_link"] = m[1]
        except ck.ClipError:
            pass
    return meta


def read_log() -> tuple[list[str], list[dict]]:
    path = ROOT / "log.csv"
    if not path.exists():
        return list(ck.LOG_COLUMNS), []
    with path.open(newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
        cols = list(r.fieldnames or ck.LOG_COLUMNS)
    for c in ck.LOG_COLUMNS:
        if c not in cols:
            cols.append(c)
    return cols, rows


def write_log(cols: list[str], rows: list[dict]) -> None:
    path = ROOT / "log.csv"
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    tmp.replace(path)


def clip_items() -> list[dict]:
    out_dir = ROOT / "out"
    if not out_dir.exists():
        return []
    _, rows = read_log()
    logs = {r.get("clip_file"): r for r in rows}
    items = []
    for mp4 in out_dir.rglob("*.mp4"):
        if not mp4.with_suffix(".txt").exists():
            continue  # still being made
        st = mp4.stat()
        r = rel(mp4)
        meta = clip_meta(mp4)
        editable = mp4.with_suffix(".json").exists() and all(meta.get(k) is not None for k in EDIT_KEYS)
        items.append({"file": r, "name": mp4.name, "url": file_url(mp4), "folder": mp4.parent.name,
                      "practice": mp4.parent.name == "practice" or mp4.name.startswith("PRACTICE-"),
                      "mtime": st.st_mtime, "size_mb": round(st.st_size / 1e6, 1), "meta": public_meta(meta),
                      "log": logs.get(r), "editable": editable})
    items.sort(key=lambda x: -x["mtime"])
    return items


def moments_view(key: str) -> dict:
    st = ck.load_state()
    v = st["streams"].get(key)
    if not v:
        raise NotFound("That stream hasn't been analysed.")
    mf = moments_path(v)
    data = None
    if mf and mf.with_suffix(".json").exists():
        data = json.loads(mf.with_suffix(".json").read_text())
    if data is None:  # analysed before the page existed: rebuild what we can
        src = ck.source_of(v["url"])
        ranking = ""
        if mf:
            m = re.search(r"^\*\*(Ranked[^\n]*?)\*\*\s*$", mf.read_text(), re.M)
            ranking = m[1] if m else ""
        data = {"key": key, "kind": src["kind"], "id": src.get("id", ""), "url": v["url"],
                "streamer": v.get("streamer", ""), "display": v.get("streamer", ""), "title": v.get("title", ""),
                "day": v.get("date", ""), "range": v.get("range"), "ranking": ranking, "ai": False, "partial": True,
                "moments": [{"n": i, "start": m["start"], "end": m["end"], "title": m.get("title") or "",
                             "hook": m.get("hook") or "", "why": "", "score": None, "spoiler": False, "chat_x": 0,
                             "loud_db": None, "chat": [], "said": [], "seg_start": max(0, m["start"] - 60),
                             "seg_end": m["end"] + 30} for i, m in enumerate(v.get("moments") or [], 1)]}
    made = [c for c in clip_items() if (c["meta"] or {}).get("source_key") == key]
    for m in data["moments"]:
        m["made"] = [{"file": c["file"], "practice": c["practice"]} for c in made
                     if c["meta"].get("start") is not None
                     and c["meta"]["start"] < m["end"] and m["start"] < (c["meta"].get("end") or c["meta"]["start"] + 1)]
    s = ck.load_streamers().get(str(data.get("streamer", "")).lower())
    data["streamer_known"] = bool(s)
    if s:
        data["display"] = s.get("display", data["streamer"])
    data["moments_file"] = str(mf) if mf else ""
    return data


# ───────────────────────────── streamers.yaml edits ─────────────────────────────

NAME_LINE = re.compile(r"^(\s*)-\s+name:\s*['\"]?([^'\"#\s]+)['\"]?\s*(#.*)?$")


def ystr(s) -> str:
    return json.dumps(str(s), ensure_ascii=False)  # a JSON string is a valid YAML string


def edit_streamers(transform, check) -> None:
    """Edit streamers.yaml as text (so its comments survive), then make sure it still reads correctly."""
    path = ROOT / "streamers.yaml"
    text = path.read_text() if path.exists() else "streamers:\n"
    new = transform(text)
    try:
        data = yaml.safe_load(new) or {}
    except yaml.YAMLError as e:
        raise BadRequest(f"That change would break streamers.yaml ({e}); nothing was saved.")
    found = {str(s["name"]).lower(): s for s in data.get("streamers", []) if isinstance(s, dict) and s.get("name")}
    if not check(found):
        raise BadRequest("That change didn't come out right in streamers.yaml; nothing was saved. Edit it by hand.")
    if path.exists():
        shutil.copyfile(path, path.with_name("streamers.yaml.bak"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(new)
    tmp.replace(path)


def block_of(lines: list[str], name: str) -> tuple[int, int, int]:
    heads = [(i, m) for i, ln in enumerate(lines) if (m := NAME_LINE.match(ln))]
    for j, (i, m) in enumerate(heads):
        if m[2].lower() == name:
            end = heads[j + 1][0] if j + 1 < len(heads) else len(lines)
            return i, end, len(m[1])
    raise NotFound(f"{name} isn't in streamers.yaml.")


def set_permission(name: str, source_: str, proof: str, checked_on: str, monetize: bool) -> None:
    values = {"source": source_, "proof": ystr(proof), "checked_on": ystr(checked_on),
              "monetize": "true" if monetize else "false"}

    def transform(text: str) -> str:
        lines = text.split("\n")
        start, end, dash = block_of(lines, name)
        key_ind = dash + 2
        perm = next((i for i in range(start + 1, end) if re.match(rf"^\s{{{key_ind}}}permission:\s*(#.*)?$", lines[i])), None)
        if perm is None:
            last = max(i for i in range(start, end) if lines[i].strip() and not lines[i].strip().startswith("#"))
            ind = " " * (key_ind + 2)
            new = [" " * key_ind + "permission:"] + [f"{ind}{k}: {v}" for k, v in values.items()]
            new.insert(3, f"{ind}platforms: [youtube_shorts, instagram_reels]")
            return "\n".join(lines[: last + 1] + new + lines[last + 1:])
        kids = []
        i = perm + 1
        while i < end and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > key_ind):
            if lines[i].strip():
                kids.append(i)
            i += 1
        child_ind = (len(lines[kids[0]]) - len(lines[kids[0]].lstrip())) if kids else key_ind + 2
        insert_at = (kids[-1] + 1) if kids else perm + 1
        extra = []
        for k, v in values.items():
            hit = next((n for n in kids if re.match(rf"^\s*{k}:", lines[n])), None)
            if hit is not None:
                lines[hit] = " " * child_ind + f"{k}: {v}"
            else:
                extra.append(" " * child_ind + f"{k}: {v}")
        return "\n".join(lines[:insert_at] + extra + lines[insert_at:])

    def check(found: dict) -> bool:
        p = (found.get(name) or {}).get("permission") or {}
        return (str(p.get("source")) == source_ and str(p.get("proof") or "") == proof
                and str(p.get("checked_on") or "") == checked_on and bool(p.get("monetize")) == monetize)

    edit_streamers(transform, check)


def add_streamer(f: dict) -> None:
    name = f["name"]

    def transform(text: str) -> str:
        lines = text.rstrip("\n").split("\n")
        heads = [m for ln in lines if (m := NAME_LINE.match(ln))]
        dash = len(heads[0][1]) if heads else 2
        if not any(re.match(r"^streamers:\s*(#.*)?$", ln) for ln in lines):
            lines.append("streamers:")
        d, k, kk = " " * dash, " " * (dash + 2), " " * (dash + 4)
        block = ["", f"{d}- name: {name}", f"{k}display: {ystr(f['display'])}", f"{k}channel: {ystr(f['channel'])}"]
        if f.get("youtube"):
            block.append(f"{k}youtube: {ystr(f['youtube'])}")
        block += [f"{k}credit: {ystr(f['credit'])}", f"{k}language: en"]
        if f.get("notes"):
            block.append(f"{k}notes: {ystr(f['notes'])}")
        block += [f"{k}permission:", f"{kk}source: none", f'{kk}proof: ""',
                  f"{kk}platforms: [youtube_shorts, instagram_reels]", f"{kk}monetize: false", f'{kk}checked_on: ""']
        return "\n".join(lines + block) + "\n"

    def check(found: dict) -> bool:
        s = found.get(name) or {}
        return s.get("display") == f["display"] and s.get("channel") == f["channel"]

    edit_streamers(transform, check)


# ───────────────────────────── actions ─────────────────────────────

def act_inspect(b: dict) -> dict:
    link = need_str(b, "link")
    src = source(link)
    st = ck.load_state()
    guess = ""  # twitch.tv/<name>/clip/… and kick.com/<name>/… name the streamer
    if m := re.search(r"(?:twitch\.tv|kick\.com)/([\w-]+)", link):
        h = m[1].lower()
        guess = next((n for n, s in ck.load_streamers().items() if h in ck.all_handles(s)), "")
    return {"kind": src["kind"], "key": src["key"], "url": src["url"], "id": src.get("id", ""),
            "link": source_arg(src), "analysed": src["key"] in st["streams"], "streamer": guess}


def act_find(b: dict) -> dict:
    src = source(need_str(b, "link"))
    if src["kind"] in ("twitch-clip", "kick-clip"):
        raise BadRequest("That's a single clip, not a full stream. Make it into a vertical clip directly.")
    if j := active("find", key=src["key"]):
        return j.view()
    args = ["findmoments", source_arg(src)]
    streamer = need_str(b, "streamer", 60, required=False).lower()
    if streamer:
        if streamer not in ck.load_streamers():
            raise BadRequest(f"{streamer} isn't in your streamers list.")
        args += ["--streamer", streamer]
    for k, flag in (("from", "--from"), ("to", "--to")):
        v = need_str(b, k, 20, required=False)
        if v:
            args += [flag, str(int(time_arg(v, k)))]
    if b.get("full_audio"):
        args.append("--full-audio")
    if b.get("again"):
        args.append("--again")
    label = "Find moments · " + (src.get("id") or Path(src["url"]).name)
    return submit(Job("find", label, args, "find", {"key": src["key"], "url": src["url"]})).view()


def act_make(b: dict) -> dict:
    src = source(need_str(b, "link"))
    name = need_str(b, "streamer", 60).lower()
    s = ck.load_streamers().get(name)
    if not s:
        raise BadRequest(f"{name} isn't in your streamers list. Add them under Streamers & setup first.")
    practice = bool(b.get("practice")) or not ck.permission(s)[0]
    args = ["makeclip", source_arg(src)]
    start = end = None
    if b.get("start") is not None and b.get("end") is not None:
        start, end = round(time_arg(b["start"], "start")), round(time_arg(b["end"], "end"))
        if end <= start:
            raise BadRequest("The end must be after the start.")
        if end - start > 180:
            raise BadRequest("Shorts and Reels max out at 3 minutes. Pick a shorter part (20–45 s works best).")
        args += [str(start), str(end)]
    args.append(name)
    hook = re.sub(r"\s+", " ", need_str(b, "hook", 120, required=False))
    title = re.sub(r"\s+", " ", need_str(b, "title", 200, required=False))
    if hook:
        args.append(f"--hook={hook}")
    context = re.sub(r"\s+", " ", need_str(b, "context", 120, required=False))
    background = re.sub(r"\s+", " ", need_str(b, "background", 800, required=False))
    if context:
        args.append(f"--context={context}")
    if background:
        args.append(f"--background={background}")
    if title:
        args.append(f"--title={title}")
    layout = need_str(b, "layout", 20, required=False)
    if layout in ("full", "camera_top"):
        args.append(f"--layout={layout}")
    if b.get("keep_silence"):
        args.append("--keep-silence")
    if practice:
        args.append("--practice")
    if b.get("again"):
        args.append("--again")
    when = f"{ck.fmt_time(start)}–{ck.fmt_time(end)}" if start is not None else "whole clip"
    label = f"Make clip · {s.get('display', name)} {when}" + (" (practice)" if practice else "")
    meta = {"key": src["key"], "ref": need_str(b, "ref", 200, required=False), "streamer": name,
            "start": start, "end": end, "practice": practice}
    return submit(Job("make", label, args, "make", meta)).view()


def act_frame(b: dict) -> dict:
    src = source(need_str(b, "link"))
    t = time_arg(need_str(b, "time", 20), "frame")
    out = FRAME_DIR / f"{ck.slug(src['key'], 40)}-{int(t)}-{uuid.uuid4().hex[:6]}.png"
    return submit(Job("frame", f"Grab a frame at {ck.fmt_time(t)}", ["frame", source_arg(src), str(int(t)), str(out)],
                      None, {"name": need_str(b, "name", 60, required=False)})).view()


def act_camera(b: dict) -> dict:
    name = need_str(b, "name", 60).lower()
    if name not in ck.load_streamers():
        raise BadRequest(f"{name} isn't in your streamers list.")
    if b.get("none"):
        args = ["camera", name, "--none"]
    else:
        frame = safe_path(need_str(b, "frame", 400), ("inbox/ui-frames",))
        if not frame.exists():
            raise BadRequest("That frame is gone; grab it again.")
        try:
            box = [int(round(float(x))) for x in b.get("box") or []]
        except (TypeError, ValueError):
            box = []
        if len(box) != 4 or box[2] < 10 or box[3] < 10:
            raise BadRequest("Drag a box around the camera first.")
        args = ["camera", name, "--frame", str(frame), "--box", ",".join(map(str, box)),
                "--set-from", need_str(b, "set_from", 600, required=False) or "the Clipping page"]
    env = {**os.environ, "CLIPKIT_ROOT": str(ROOT)}
    res = subprocess.run([sys.executable, "-I", CLIPKIT, *args], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=120)
    if res.returncode:
        raise BadRequest(error_text((res.stdout + res.stderr).splitlines()))
    return {"streamers": streamers_view()}


def act_permission(b: dict) -> dict:
    name = need_str(b, "name", 60).lower()
    src = need_str(b, "source", 40)
    if src not in SOURCES:
        raise BadRequest("Pick where the permission comes from.")
    proof = need_str(b, "proof", 500, required=False)
    checked = need_str(b, "checked_on", 20, required=False)
    if checked and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked):
        raise BadRequest("Write the date as YYYY-MM-DD.")
    if src != "none" and not proof:
        raise BadRequest("Add the proof: a link or a screenshot path you could show if anyone asks.")
    if src != "none" and not checked:
        checked = dt.date.today().isoformat()
    set_permission(name, src, proof, checked, bool(b.get("monetize")))
    return {"streamers": streamers_view()}


def act_add_streamer(b: dict) -> dict:
    name = need_str(b, "name", 30).lower().lstrip("@")
    if not re.fullmatch(r"[a-z0-9_]{2,30}", name):
        raise BadRequest("The short name can use only lowercase letters, numbers and _ (like kaicenat).")
    if name in ck.load_streamers():
        raise BadRequest(f"{name} is already in your list.")
    channel = need_str(b, "channel", 300).rstrip("/")
    if not channel.startswith("http"):
        channel = "https://" + channel
    if not re.match(r"https://(www\.)?(twitch\.tv|kick\.com|youtube\.com)/\S+$", channel):
        raise BadRequest("The channel must be a https://twitch.tv/…, kick.com/… or youtube.com/… link.")
    yt = need_str(b, "youtube", 100, required=False)
    if yt and not yt.startswith("@"):
        yt = "@" + yt.rsplit("/", 1)[-1].lstrip("@")
    credit = need_str(b, "credit", 100, required=False) or "@" + name
    if not credit.startswith("@"):
        credit = "@" + credit
    add_streamer({"name": name, "display": need_str(b, "display", 80, required=False) or name, "channel": channel,
                  "youtube": yt, "credit": credit, "notes": need_str(b, "notes", 500, required=False)})
    return {"streamers": streamers_view()}


def act_record(b: dict) -> dict:
    name = need_str(b, "name", 60).lower()
    s = ck.load_streamers().get(name)
    if not s or not s.get("channel"):
        raise BadRequest(f"{name} has no channel link in streamers.yaml.")
    if j := active("record", streamer=name):
        return j.view()
    return submit(Job("record", f"Record live · {s.get('display', name)}", ["record", name], None,
                      {"streamer": name})).view()


def act_context(b: dict) -> dict:
    """Read the minutes before a moment: a context line, where the setup starts, background for the post text."""
    src = source(need_str(b, "link"))
    start = time_arg(b.get("start") if b.get("start") is not None else 0, "start")
    end = time_arg(b.get("end") if b.get("end") is not None else 0, "end")
    ref = need_str(b, "ref", 200, required=False)
    if j := active("context", ref=ref):
        return j.view()
    args = ["context", source_arg(src), str(int(start)), str(int(end))]
    name = need_str(b, "streamer", 60, required=False).lower()
    if name:
        args += ["--streamer", name]
    hook = re.sub(r"\s+", " ", need_str(b, "hook", 120, required=False))
    if hook:
        args.append(f"--hook={hook}")
    return submit(Job("context", f"Context · {ck.fmt_time(start)}", args, "context", {"ref": ref, "key": src["key"]})).view()


def act_live(b: dict) -> dict:
    name = need_str(b, "name", 60).lower()
    s = ck.load_streamers().get(name)
    if not s:
        raise BadRequest(f"{name} isn't in your streamers list.")
    if not ck.live_places(s):
        raise BadRequest(f"{s.get('display', name)} has no Twitch, Kick or YouTube channel in streamers.yaml.")
    if j := active("live", streamer=name):
        return j.view()
    every = b.get("every") or 20
    try:
        every = max(5, min(120, int(every)))
    except (TypeError, ValueError):
        every = 20
    return submit(Job("live", f"Follow live · {s.get('display', name)}", ["live", name, "--every", str(every)], None,
                      {"streamer": name, "every": every})).view()


def act_clip_save(b: dict) -> dict:
    mp4 = safe_path(need_str(b, "file", 400), ("out",))
    if mp4.suffix != ".mp4" or not mp4.exists():
        raise NotFound("That clip is gone.")
    meta = clip_meta(mp4)
    meta["title"] = re.sub(r"\s+", " ", need_str(b, "title", 300, required=False))
    for k in ("yt_description", "ig_caption"):
        if k in b:
            meta[k] = need_str(b, k, 5000, required=False).replace("\r\n", "\n")
    mp4.with_suffix(".txt").write_text(ck.clip_txt(meta))
    mp4.with_suffix(".json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    ck.set_log_title(rel(mp4), meta["title"])
    return {"ok": True, "meta": public_meta(meta)}


def clip_json(b: dict) -> tuple[Path, dict]:
    mp4 = safe_path(need_str(b, "file", 400), ("out",))
    js = mp4.with_suffix(".json")
    if mp4.suffix != ".mp4" or not mp4.exists():
        raise NotFound("That clip is gone.")
    meta = json.loads(js.read_text()) if js.exists() else {}
    if not all(meta.get(k) is not None for k in EDIT_KEYS):
        raise BadRequest("This clip was made before clips kept their details. Make it again (same times) to edit it.")
    return mp4, meta


def fix_rules(name: str) -> list[dict]:
    f = ck.load_fixes()
    out = [{"from": r.get("from", ""), "to": r.get("to", ""), "scope": "all"} for r in f["all"] if isinstance(r, dict)]
    out += [{"from": r.get("from", ""), "to": r.get("to", ""), "scope": name}
            for r in (f["streamers"].get(name) or []) if isinstance(r, dict)]
    return out


def captions_view(relpath: str) -> dict:
    mp4, meta = clip_json({"file": relpath})
    name = str(meta.get("streamer", ""))
    s = ck.load_streamers().get(name) or {}
    out = {"file": rel(mp4), "url": file_url(mp4), "streamer": name, "display": s.get("display", meta.get("display", name)),
           "rules": fix_rules(name), "edited": bool(meta.get("captions_edited")), "ready": bool(meta.get("words")),
           "hook": meta.get("hook", ""), "context": meta.get("context", "")}
    if out["ready"]:
        cfg = ck.load_config()
        _, _, lines = ck.caption_lines(meta["words"], meta.get("keep") or [[0.0, 1e9]], cfg["captions"])
        out["lines"] = [{"i": ln["i"], "start": ln["start"], "end": ln["end"], "text": ln["text"]} for ln in lines]
    return out


def act_prepare(b: dict) -> dict:
    mp4, _ = clip_json(b)
    if j := active("captions", file=rel(mp4)):
        return j.view()
    return submit(Job("captions", f"Read the captions · {mp4.name}", ["captions", str(mp4)], None,
                      {"file": rel(mp4)})).view()


def act_recaption(b: dict) -> dict:
    mp4, meta = clip_json(b)
    if j := active("recaption", file=rel(mp4)):
        return j.view()
    args = ["recaption", str(mp4)]
    for k in ("hook", "context"):  # changed in the caption editor ("" takes it off)
        if b.get(k) is not None:
            v = re.sub(r"\s+", " ", need_str(b, k, 120, required=False))
            args.append(f"--{k}={v}")
    if b.get("reset"):
        args.append("--reset")
    else:
        if not meta.get("words"):
            raise BadRequest("Open the captions first.")
        edits = b.get("edits") or {}
        replace = [r for r in (b.get("replace") or []) if isinstance(r, dict)][:50]
        if not isinstance(edits, dict):
            raise BadRequest("Bad edits.")
        cfg = ck.load_config()
        _, _, lines = ck.caption_lines(meta["words"], meta.get("keep") or [[0.0, 1e9]], cfg["captions"])
        ed = {}
        for k, v in edits.items():
            try:
                i = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(lines):
                ed[i] = re.sub(r"\s+", " ", str(v or "")).strip()[:200]
        words = ck.apply_line_edits(meta["words"], lines, ed)
        words, _ = ck.apply_rules(words, [{"from": str(r.get("from", ""))[:100], "to": str(r.get("to", ""))[:100]}
                                          for r in replace])
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        path = JOB_DIR / f"words-{uuid.uuid4().hex[:10]}.json"
        path.write_text(json.dumps(words, ensure_ascii=False))
        args += ["--words", str(path)]
    label = ("Undo caption changes · " if b.get("reset") else "Re-make with new captions · ") + mp4.name
    return submit(Job("recaption", label, args, "make", {"file": rel(mp4)})).view()


def act_posttext(b: dict) -> dict:
    mp4, _ = clip_json(b)
    if j := active("posttext", file=rel(mp4)):
        return j.view()
    return submit(Job("posttext", f"Write post text with AI · {mp4.name}", ["posttext", str(mp4)], None,
                      {"file": rel(mp4)})).view()


def act_fix_add(b: dict) -> dict:
    name = need_str(b, "streamer", 60, required=False).lower()
    frm = re.sub(r"\s+", " ", need_str(b, "from", 100)).strip()
    to = re.sub(r"\s+", " ", need_str(b, "to", 100)).strip()
    if name and name not in ck.load_streamers():
        raise BadRequest(f"{name} isn't in your streamers list.")
    d = ck.load_fixes()
    target = d["streamers"].setdefault(name, []) if name else d["all"]
    target[:] = [r for r in target if not (isinstance(r, dict) and str(r.get("from", "")).lower() == frm.lower())]
    target.append({"from": frm, "to": to})
    ck.save_fixes(d)
    return {"rules": fix_rules(name)}


def act_fix_remove(b: dict) -> dict:
    name = need_str(b, "streamer", 60, required=False).lower()
    scope = need_str(b, "scope", 60, required=False).lower()
    frm = need_str(b, "from", 100).lower()
    d = ck.load_fixes()
    target = d["all"] if scope == "all" else (d["streamers"].get(scope) or [])
    target[:] = [r for r in target if not (isinstance(r, dict) and str(r.get("from", "")).lower() == frm)]
    d["streamers"] = {k: v for k, v in d["streamers"].items() if v}
    ck.save_fixes(d)
    return {"rules": fix_rules(name)}


def act_ai_resume(b: dict) -> dict:
    ck.ai_resume()
    return status_view()


def run_clipkit(args: list[str], timeout: int = 120) -> None:
    env = {**os.environ, "CLIPKIT_ROOT": str(ROOT)}
    res = subprocess.run([sys.executable, "-I", CLIPKIT, *args], cwd=str(ROOT), env=env,
                         capture_output=True, text=True, timeout=timeout)
    if res.returncode:
        raise BadRequest(error_text((res.stdout + res.stderr).splitlines()))


def brand_view() -> dict:
    s = ck.brand_settings()
    b = ck.current_brand()
    want = b["id"] if b else ""
    clips = [p for p in (ROOT / "out").rglob("*.mp4") if p.with_suffix(".txt").exists() and ".making" not in p.name] \
        if (ROOT / "out").exists() else []
    cut, prev = ck.BRAND_DIR / "logo-cut.png", ck.BRAND_DIR / "preview.png"
    return {**s, "sizes": list(ck.BRAND_SIZES), "spots": list(ck.BRAND_SPOTS),
            "logo": file_url(cut) if cut.exists() else "", "preview": file_url(prev) if prev.exists() else "",
            "clips": len(clips), "outdated": sum(1 for p in clips if ck.clip_brand(p) != want)}


def act_brand(b: dict) -> dict:
    d = {}
    if "enabled" in b:
        d["enabled"] = bool(b["enabled"])
    if b.get("position") in ck.BRAND_SPOTS:
        d["position"] = b["position"]
    if b.get("size") in ck.BRAND_SIZES:
        d["size"] = b["size"]
    ck.save_brand_settings(d)
    if (ck.BRAND_DIR / "logo-cut.png").exists():
        run_clipkit(["brand", "preview"])
    return brand_view()


def act_brand_logo(b: dict) -> dict:
    import base64
    data = str(b.get("data") or "")
    m = re.match(r"data:image/(png|jpeg|jpg|webp);base64,(.+)$", data, re.S)
    if not m:
        raise BadRequest("Pick a PNG, JPG or WebP picture of your logo.")
    raw = base64.b64decode(m[2])
    if len(raw) > 15_000_000:
        raise BadRequest("That picture is bigger than 15 MB.")
    ck.BRAND_DIR.mkdir(parents=True, exist_ok=True)
    up = ck.BRAND_DIR / f"_new-logo.{'jpg' if m[1] in ('jpeg', 'jpg') else m[1]}"
    up.write_bytes(raw)
    try:
        run_clipkit(["brand", "logo", str(up)])
    finally:
        up.unlink(missing_ok=True)
    return brand_view()


def act_brand_apply(b: dict) -> dict:
    if j := active("brand"):
        return j.view()
    return submit(Job("brand", "Update the logo on existing clips", ["brand", "apply"], "make", {})).view()


def act_clip_log(b: dict) -> dict:
    mp4 = safe_path(need_str(b, "file", 400), ("out",))
    fields = b.get("fields") or {}
    if not isinstance(fields, dict):
        raise BadRequest("Bad fields.")
    cols, rows = read_log()
    r = next((x for x in rows if x.get("clip_file") == rel(mp4)), None)
    if r is None:
        meta = clip_meta(mp4) if mp4.exists() else {}
        r = {"made_on": meta.get("made_on", ""), "clip_file": rel(mp4), "streamer": meta.get("streamer", ""),
             "source_link": meta.get("stream_link", ""), "start": ck.fmt_time(meta["start"]) if meta.get("start") is not None else "",
             "end": ck.fmt_time(meta["end"]) if meta.get("end") is not None else "", "title": meta.get("title", "")}
        rows.append(r)
    for k in LOG_EDITABLE:
        if k in fields:
            r[k] = re.sub(r"[\r\n]+", " ", str(fields[k] or "")).strip()[:500]
    write_log(cols, rows)
    return {"ok": True, "log": r}


def to_bin(files: list[Path]) -> None:
    """Move files to the Mac's Bin, so a delete can still be undone from Finder."""
    if not files:
        return
    if Path("/usr/bin/trash").exists():
        res = subprocess.run(["/usr/bin/trash", *map(str, files)], capture_output=True, text=True)
        if res.returncode:
            raise BadRequest(f"Couldn't move it to the Bin: {res.stderr.strip() or res.stdout.strip()}")
    else:
        bin_dir = Path.home() / ".Trash"
        for p in files:
            shutil.move(str(p), str(bin_dir / f"{p.stem}-{uuid.uuid4().hex[:4]}{p.suffix}"))


def act_clip_trash(b: dict) -> dict:
    mp4 = safe_path(need_str(b, "file", 400), ("out",))
    if mp4.suffix != ".mp4":
        raise BadRequest("Only clips can be deleted here.")
    orig = ck.UNBRANDED / rel(mp4).replace("/", "__")  # its copy without the logo, if it has one
    files = [p for p in (mp4, mp4.with_suffix(".txt"), mp4.with_suffix(".json"), orig) if p.exists()]
    if not files:
        raise NotFound("That clip is already gone.")
    to_bin(files)
    st = ck.load_state()
    keep = [c for c in st["clips"] if Path(c.get("file", "")).resolve() != mp4]
    if len(keep) != len(st["clips"]):
        st["clips"] = keep
        ck.save_state(st)
    cols, rows = read_log()
    kept = [r for r in rows if not (r.get("clip_file") == rel(mp4) and not r.get("youtube_url") and not r.get("instagram_url"))]
    if len(kept) != len(rows):
        write_log(cols, kept)
    return {"ok": True}


def remove_streamer(name: str) -> None:
    before = set(ck.load_streamers())

    def transform(text: str) -> str:
        lines = text.split("\n")
        start, end, dash = block_of(lines, name)
        # keep the blank lines and comments that lead into the next streamer; drop the gap before this one
        cut = end
        while cut > start + 1 and (not lines[cut - 1].strip() or (lines[cut - 1].lstrip().startswith("#")
                                                                    and len(lines[cut - 1]) - len(lines[cut - 1].lstrip()) <= dash)):
            cut -= 1
        if start > 0 and not lines[start - 1].strip():
            start -= 1
        return "\n".join(lines[:start] + lines[cut:])

    def check(found: dict) -> bool:
        return set(found) == before - {name}

    edit_streamers(transform, check)


def act_streamer_delete(b: dict) -> dict:
    name = need_str(b, "name", 60).lower()
    s = ck.load_streamers().get(name)
    if not s:
        raise NotFound(f"{name} isn't in your streamers list.")
    for kind, what in (("live", "Following live"), ("record", "Recording")):
        if active(kind, streamer=name):
            raise BadRequest(f"{what} is still on for {s.get('display', name)}. Press Stop first.")
    remove_streamer(name)
    lay = ROOT / "layouts"  # their camera box and its pictures
    to_bin([p for p in (lay / f"{name}.json", lay / f"{name}-preview.png", lay / f"{name}-frame.png",
                        lay / f"{name}-frame-display.png") if p.exists()])
    return {"streamers": streamers_view()}


def act_stream_delete(b: dict) -> dict:
    key = need_str(b, "key", 200)
    st = ck.load_state()
    v = st["streams"].get(key)
    if not v:
        raise NotFound("That stream is already gone.")
    if active("find", key=key):
        raise BadRequest("Moments are still being found in this stream. Stop that first.")
    if v.get("live") and active("live", streamer=v.get("streamer")):
        raise BadRequest("This stream is being followed live. Press Stop on it (Streamers & setup) first.")
    # the moments files go to the Bin; the downloads made to analyse it are deleted (they're only a cache).
    # Clip pieces (clip-*) stay, so Edit captions on clips made from this stream still works without downloading.
    mf = moments_path(v)
    files = [p for p in ({mf, mf.with_suffix(".json"), mf.with_suffix(".md")} if mf else set()) if p.exists()]
    to_bin(sorted(files))
    work = ROOT / "inbox" / key
    if re.fullmatch(r"[\w.-]+", key) and work.is_dir():
        for p in work.iterdir():
            if p.name.startswith("clip-"):
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    for p in (ROOT / "inbox" / "live" / "previews").glob(f"{key}-*.mp4"):
        p.unlink(missing_ok=True)
    st = ck.load_state()
    st["streams"].pop(key, None)
    ck.save_state(st)
    return {"streams": streams_view()}


def act_reveal(b: dict) -> dict:
    p = safe_path(need_str(b, "file", 400), ("out", "layouts", "inbox", "moments"))
    if not p.exists():
        raise NotFound("That file is gone.")
    subprocess.run(["open", "-R", str(p)])
    return {"ok": True}


def act_open(b: dict) -> dict:
    what = need_str(b, "what", 20)
    targets = {"folder": (ROOT, []), "out": (ROOT / "out", []), "streamers": (ROOT / "streamers.yaml", ["-t"]),
               "config": (ROOT / "config.yaml", ["-t"]), "readme": (ROOT / "README.md", ["-t"]),
               "log": (ROOT / "log.csv", [])}
    if what not in targets:
        raise BadRequest("Unknown thing to open.")
    path, flags = targets[what]
    if not path.exists():
        if what == "out":
            path.mkdir(parents=True, exist_ok=True)
        else:
            raise NotFound(f"{path.name} doesn't exist yet.")
    subprocess.run(["open", *flags, str(path)])
    return {"ok": True}


POST_ROUTES = {
    "/api/inspect": act_inspect, "/api/find": act_find, "/api/make": act_make, "/api/frame": act_frame,
    "/api/camera": act_camera, "/api/permission": act_permission, "/api/streamer": act_add_streamer,
    "/api/record": act_record, "/api/clip/save": act_clip_save, "/api/clip/log": act_clip_log,
    "/api/clip/trash": act_clip_trash, "/api/reveal": act_reveal, "/api/open": act_open,
    "/api/clip/prepare": act_prepare, "/api/clip/recaption": act_recaption, "/api/clip/posttext": act_posttext,
    "/api/fixes": act_fix_add, "/api/fixes/remove": act_fix_remove, "/api/ai/resume": act_ai_resume,
    "/api/brand": act_brand, "/api/brand/logo": act_brand_logo, "/api/brand/apply": act_brand_apply,
    "/api/live": act_live, "/api/context": act_context,
    "/api/streamer/delete": act_streamer_delete, "/api/stream/delete": act_stream_delete,
}


# ───────────────────────────── HTTP ─────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "clipui"

    def log_message(self, fmt, *args):  # keep the Terminal window quiet
        pass

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "").lower() in {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=float).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, msg: str) -> None:
        self._json({"error": msg}, status)

    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "Open the page at http://localhost:%d" % PORT)
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                html = (UI_DIR / "index.html").read_text().replace("__TOKEN__", TOKEN)
                return self._send_bytes(html.encode(), TYPES[".html"])
            if path.startswith("/ui/"):
                p = (UI_DIR / path[4:]).resolve()
                if not p.is_relative_to(UI_DIR.resolve()) or not p.is_file():
                    raise NotFound("Not found.")
                return self._send_bytes(p.read_bytes(), TYPES.get(p.suffix, "application/octet-stream"))
            if path.startswith("/files/"):
                p = safe_path(path[len("/files/"):])
                if not p.is_file():
                    raise NotFound("Not found.")
                return self._send_file(p)
            if path == "/api/ping":
                return self._json({"app": "clipui"})
            if path == "/api/status":
                return self._json(status_view())
            if path == "/api/streamers":
                return self._json(streamers_view())
            if path == "/api/streams":
                return self._json(streams_view())
            if path == "/api/moments":
                return self._json(moments_view((qs.get("key") or [""])[0]))
            if path == "/api/clips":
                return self._json(clip_items())
            if path == "/api/clip/captions":
                return self._json(captions_view((qs.get("file") or [""])[0]))
            if path == "/api/brand":
                return self._json(brand_view())
            if path == "/api/jobs":
                with JOBS_LOCK:
                    jobs = list(JOBS.values())
                return self._json([j.view() for j in sorted(jobs, key=lambda j: -j.created)])
            raise NotFound("Not found.")
        except NotFound as e:
            self._error(404, str(e))
        except BadRequest as e:
            self._error(400, str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._error(500, f"Something went wrong: {e}")

    def do_POST(self):
        if not self._host_ok():
            return self._error(403, "Wrong host.")
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self._error(403, "Requests from other sites aren't allowed.")
        if self.headers.get("X-Clip-Token") != TOKEN:
            return self._error(403, "This page is out of date. Reload it.")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._error(415, "Send JSON.")
        try:
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            if n > (25_000_000 if path == "/api/brand/logo" else 1_000_000):
                raise BadRequest("Too much data.")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise BadRequest("Send a JSON object.")
            if m := re.fullmatch(r"/api/jobs/([0-9a-f]{12})/stop", path):
                job = JOBS.get(m[1])
                if not job:
                    raise NotFound("That job is gone.")
                stop_job(job)
                return self._json(job.view())
            if path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.3), os.kill(os.getpid(), signal.SIGTERM)),
                                 daemon=True).start()
                return
            fn = POST_ROUTES.get(path)
            if not fn:
                raise NotFound("Not found.")
            return self._json(fn(body))
        except NotFound as e:
            self._error(404, str(e))
        except BadRequest as e:
            self._error(400, str(e))
        except json.JSONDecodeError:
            self._error(400, "That wasn't valid JSON.")
        except subprocess.TimeoutExpired:
            self._error(504, "That took too long. Try again.")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._error(500, f"Something went wrong: {e}")

    def _send_bytes(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, p: Path) -> None:
        size = p.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and (m := re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())):
            a, b = m.groups()
            if a:
                start, end = int(a), min(int(b), size - 1) if b else size - 1
            elif b:
                start, end = max(0, size - int(b)), size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", TYPES.get(p.suffix.lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with p.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(262144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=0.6) as r:
            return json.load(r).get("app") == "clipui"
    except Exception:
        return False


def main() -> int:
    global PORT
    ap = argparse.ArgumentParser(description="Clipping page")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    a = ap.parse_args()
    srv = None
    for port in range(a.port, a.port + 10):
        if already_running(port):
            url = f"http://localhost:{port}/"
            print(f"Clipping is already running: {url}")
            if not a.no_open:
                webbrowser.open(url)
            return 0
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            PORT = port
            break
        except OSError:
            continue
    if srv is None:
        print("No free port between %d and %d." % (a.port, a.port + 9))
        return 1
    for q in LANES.values():
        threading.Thread(target=lane_worker, args=(q,), daemon=True).start()

    def bye(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGHUP, bye)  # Terminal window closed

    url = f"http://localhost:{PORT}/"
    print(f"\n  Clipping is running at {url}\n"
          f"  Keep this window open while you work. Close it (or press Ctrl+C) to stop.\n", flush=True)
    if not a.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        running = [j for j in JOBS.values() if j.status in ("queued", "running")]
        for j in running:
            stop_job(j)
        if running:
            print(f"  Stopping {len(running)} job(s)...", flush=True)
            time.sleep(1.5)
            for j in running:
                if j.proc and j.proc.poll() is None:
                    try:
                        os.killpg(j.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        srv.server_close()
        print("  Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
