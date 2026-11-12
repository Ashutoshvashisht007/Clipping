#!/usr/bin/env python3
"""clipkit: find the best moments in a stream and turn them into captioned vertical clips.

Run it through the launchers in ~/Developer/Clipping/bin (they use the right Python):

  findmoments <stream link or file> [--streamer NAME] [--from T] [--to T] [--top N]
              [--full-audio] [--again]
  makeclip    <link or file> [start end] <streamer> [--hook TEXT] [--title TEXT]
              [--layout full|camera_top] [--keep-silence] [--practice] [--again]
  clip camera <streamer> <link or file> <time> [--box X,Y,W,H | --none]
  clip record <streamer>
  clip check

Times can be written 1:23:45, 83:45, 5025 or 1h23m45s.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

import certifi
import numpy as np
import yaml

# The project folder is wherever this file lives (bin/ is inside it), so the folder can be moved freely.
ROOT = Path(os.environ.get("CLIPKIT_ROOT") or Path(__file__).resolve().parent.parent)
FFBIN = Path("/opt/homebrew/opt/ffmpeg-full/bin")  # the full build: it has the caption (libass) filter
VENV_BIN = ROOT / ".venv" / "bin"
os.environ["PATH"] = f"{FFBIN}:{VENV_BIN}:{os.environ.get('PATH', '')}"
FFMPEG, FFPROBE = str(FFBIN / "ffmpeg"), str(FFBIN / "ffprobe")
# Run as modules of this Python, not via .venv/bin scripts (those have the folder's path baked in).
YTDLP, STREAMLINK = [sys.executable, "-m", "yt_dlp"], [sys.executable, "-m", "streamlink"]
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
FONTS_DIR = "/System/Library/Fonts/Supplemental"

TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's public web-player id (no login)
TWITCH_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"

OUT_W, OUT_H = 1080, 1920
WIN = 10  # seconds per analysis window
# Chat reaction is the strongest signal; volume only helps, and is capped so a loud game
# sound can't outrank a moment chat actually reacted to.
CHAT_WEIGHT, LOUD_WEIGHT, LOUD_CAP = 0.7, 0.3, 4.0

REACT_RE = re.compile(
    r"(\b(LUL|LULW|KEKW|KEKL|OMEGALUL|LMA+O+|LMF+A+O+|LO+L|ICANT|PogU?|POGGERS|PagMan|monkaS|monkaW|"
    r"NOWAY|NO WAY|HOLY|OMG|W+|L+|BRO+)\b|😂|🤣|💀|😭|🔥|\?\?\?)",
    re.IGNORECASE,
)
CLIP_RE = re.compile(r"\bclip\b", re.IGNORECASE)

DEFAULT_CONFIG = {
    "rank": {
        "provider": "none",
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "model": "glm-5.3-flash",
        "key_from": "keychain:opencode-go",
        "max_tokens": 4000,
        "price_per_million": {"input": 0.15, "output": 0.50},
    },
    "captions": {
        "model_dir": "models/parakeet-tdt-0.6b-v3",
        "font": "Arial Black",
        "size": 84,
        "max_words": 3,
        "max_chars": 14,
        "color": "#FFFFFF",
        "highlight": "#FFE600",
    },
    "clips": {"cut_silence": True, "hashtags": ["#GTA6", "#GTAVI", "#gaming", "#shorts"], "ai_text": True},
    "findmoments": {"candidates": 25, "top": 10},
    "keep_inbox_days": 7,
}

SYSTEM_PROMPT = """You are the editor of an English YouTube Shorts / Instagram Reels page that posts short clips of livestreamers (mainly GTA 6). From the candidate moments below, pick the ones that will work as a 15-45 second vertical clip for people who have never watched this streamer.

Good clips: a funny, shocking, chaotic, rage, clutch or wholesome moment with a clear setup and a payoff (usually the streamer's reaction) that makes sense on its own.
Skip: reading chat, ads or sponsor segments, music playing, long silences, inside jokes that need context, and anything that only works because of on-screen detail the transcript can't show.
Set "spoiler": true for anything that reveals GTA 6 story events, missions or cutscenes.

Each candidate lists how much faster chat moved than normal, how much louder the stream got, what chat said, and a transcript with stream timestamps. Chat and volume peaks usually come just AFTER the moment, so start the clip before the peak to include the setup.

Reply with JSON only, no other text:
{"moments":[{"id":<candidate id>,"score":<1-10>,"title":"<5-9 word clip title>","why":"<one line: what happens>","start":"H:MM:SS","end":"H:MM:SS","hook":"<on-screen hook line, max 8 words>","spoiler":false}]}
Order best first. Return at most {top}; leave out weak ones. start and end must be inside that candidate's transcript time range, and each clip must be 15-45 seconds long."""


class ClipError(Exception):
    pass


class AIError(ClipError):
    """An AI call failed. kind: limit (usage limit), down (unreachable), auth (key refused), config (bad model/URL)."""

    def __init__(self, msg: str, kind: str, retry_after: float | None = None):
        super().__init__(msg)
        self.kind, self.retry_after = kind, retry_after


class AISkip(ClipError):
    """The AI wasn't called: it's off, has no key, or is paused."""


# ───────────────────────────── small helpers ─────────────────────────────

def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    print(f"• {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"! {msg}", flush=True)


def report(**data) -> None:
    """Tell the Clipping page what a command produced (the page sets CLIPKIT_RESULT to a file path)."""
    path = os.environ.get("CLIPKIT_RESULT")
    if path:
        Path(path).write_text(json.dumps(data, default=float))


def run(cmd, *, cwd=None, live=False, check=True) -> str:
    cmd = [str(c) for c in cmd]
    if live:
        res = subprocess.run(cmd, cwd=cwd)
        if check and res.returncode:
            raise ClipError(f"{Path(cmd[0]).name} failed (exit {res.returncode})")
        return ""
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and res.returncode:
        tail = "\n    ".join((res.stderr or res.stdout or "").strip().splitlines()[-6:])
        raise ClipError(f"{Path(cmd[0]).name} failed:\n    {tail}")
    return res.stdout


def parse_time(s) -> float:
    s = str(s).strip().lower()
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", s)
    if s and m and any(m.groups()):
        h, mi, se = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)
    parts = s.split(":")
    if 1 <= len(parts) <= 3 and all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
        return total
    raise ValueError(f"can't read the time '{s}' (use 1:23:45, 83:45, 5025 or 1h23m45s)")


def is_time(s) -> bool:
    try:
        parse_time(s)
        return True
    except ValueError:
        return False


def fmt_time(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_dash(sec: float) -> str:
    return fmt_time(sec).replace(":", "-")


def twitch_t(sec: float) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def ass_color(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def even(x: float) -> int:
    return max(2, int(x) // 2 * 2)


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n] or "x"


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config() -> dict:
    path = ROOT / "config.yaml"
    user = yaml.safe_load(path.read_text()) if path.exists() else {}
    return deep_merge(DEFAULT_CONFIG, user or {})


def load_streamers() -> dict:
    path = ROOT / "streamers.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return {str(s["name"]).lower(): s for s in (data or {}).get("streamers", []) if s.get("name")}


def get_streamer(name: str) -> dict:
    s = load_streamers().get(str(name).lower())
    if not s:
        known = ", ".join(load_streamers()) or "none yet"
        raise ClipError(f"'{name}' isn't in streamers.yaml (known: {known}). Add them there first.")
    return s


def channel_handle(s: dict) -> str:
    return str(s.get("channel", "")).rstrip("/").split("/")[-1].lstrip("@").lower()


def all_handles(s: dict) -> set[str]:
    """The streamer's name on every platform: main channel plus optional youtube/kick handles."""
    out = {channel_handle(s), str(s.get("name", "")).lower()}
    for k in ("youtube", "kick", "twitch"):
        if s.get(k):
            out.add(str(s[k]).rstrip("/").split("/")[-1].lstrip("@").lower())
    return {h for h in out if h}


def permission(s: dict) -> tuple[bool, str]:
    p = s.get("permission") or {}
    src = str(p.get("source", "none")).strip().lower()
    proof = str(p.get("proof", "") or "").strip()
    ok = src in ("whop_campaign", "written", "public_statement") and bool(proof)
    return ok, src


STATE = ROOT / "state.json"


def load_state() -> dict:
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    st.setdefault("streams", {})
    st.setdefault("clips", [])
    return st


def save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(STATE)


def clean_inbox(cfg: dict) -> None:
    inbox = ROOT / "inbox"
    if not inbox.exists():
        return
    cutoff = time.time() - float(cfg.get("keep_inbox_days", 7)) * 86400
    for d in inbox.iterdir():
        if d.name.startswith("_"):
            continue
        if d.name == "live" and d.is_dir():
            for f in d.iterdir():
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            continue
        try:
            newest = max([p.stat().st_mtime for p in d.rglob("*")] + [d.stat().st_mtime])
        except FileNotFoundError:
            continue
        if newest < cutoff:
            shutil.rmtree(d, ignore_errors=True)


# ───────────────────────────── sources ─────────────────────────────

def source_of(target: str) -> dict:
    p = Path(target).expanduser()
    if p.exists():
        return {"kind": "file", "path": str(p.resolve()), "key": "file-" + slug(p.stem), "url": str(p.resolve())}
    u = target.strip()
    if m := re.search(r"twitch\.tv/videos/(\d+)", u):
        return {"kind": "twitch", "id": m[1], "key": f"twitch-{m[1]}", "url": f"https://www.twitch.tv/videos/{m[1]}"}
    if m := re.search(r"(?:clips\.twitch\.tv/|twitch\.tv/[\w-]+/clip/)([\w-]+)", u):
        return {"kind": "twitch-clip", "id": m[1], "key": f"twitchclip-{slug(m[1])}", "url": u.split("?")[0]}
    if m := re.search(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|live/|shorts/)|youtu\.be/)([\w-]{11})", u):
        return {"kind": "youtube", "id": m[1], "key": f"youtube-{m[1]}", "url": f"https://www.youtube.com/watch?v={m[1]}"}
    if m := re.search(r"kick\.com/([\w-]+)/videos/([\w-]+)", u):
        return {"kind": "kick", "id": m[2], "key": f"kick-{slug(m[2], 16)}", "url": u.split("?")[0]}
    if re.search(r"kick\.com/[\w-]+/clips?/|kick\.com/[\w-]+\?clip=", u):
        return {"kind": "kick-clip", "id": slug(u), "key": f"kickclip-{slug(u.split('/')[-1], 30)}", "url": u}
    raise ClipError("I don't recognise that link. Use a Twitch VOD (twitch.tv/videos/...), a Twitch clip, "
                    "a YouTube video, a Kick VOD or clip, or a video file on your Mac.")


def link_at(src: dict, sec: float) -> str:
    if src["kind"] == "twitch":
        return f"{src['url']}?t={twitch_t(sec)}"
    if src["kind"] == "youtube":
        return f"https://www.youtube.com/watch?v={src['id']}&t={int(sec)}s"
    if src["kind"] == "kick":
        return f"{src['url']}  (jump to {fmt_time(sec)})"
    return src["url"]


def probe(src: dict, fmt: str | None = None) -> dict:
    """Metadata plus direct media URL(s) for ffmpeg."""
    if src["kind"] == "file":
        out = run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", src["path"]])
        dur = float(json.loads(out)["format"]["duration"])
        return {"title": Path(src["path"]).stem, "duration": dur, "_input": [{"url": src["path"], "headers": None}]}
    cmd = [*YTDLP, "-J", "--no-warnings", "--no-playlist"] + (["-f", fmt] if fmt else []) + [src["url"]]
    info = json.loads(run(cmd))
    fmts = info.get("requested_formats") or ([info] if info.get("url") else [])
    info["_input"] = [{"url": f["url"], "headers": f.get("http_headers")} for f in fmts]
    return info


def ff_input(inp: dict, ss: float | None = None, dur: float | None = None) -> list:
    args: list = []
    if str(inp["url"]).startswith("http"):
        args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if inp.get("headers"):
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in inp["headers"].items())]
    if ss is not None:
        args += ["-ss", f"{max(0.0, ss):.3f}"]
    if dur is not None:
        args += ["-t", f"{dur:.3f}"]
    return args + ["-i", inp["url"]]


def media_info(path: Path) -> dict:
    out = run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=width,height,r_frame_rate:format=duration", "-of", "json", path])
    d = json.loads(out)
    st = (d.get("streams") or [{}])[0]
    num, _, den = str(st.get("r_frame_rate", "30/1")).partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return {"w": int(st.get("width", 0)), "h": int(st.get("height", 0)), "fps": fps,
            "duration": float((d.get("format") or {}).get("duration", 0))}


# ───────────────────────────── audio + speech ─────────────────────────────

def pcm_seconds_db(proc_stdout, rate: int = 8000) -> np.ndarray:
    """Read mono s16le PCM from a pipe; return loudness (dBFS) for each second."""
    vals: list[float] = []
    buf = b""
    step_bytes = rate * 2
    while True:
        chunk = proc_stdout.read(step_bytes * 60)
        if not chunk:
            break
        buf += chunk
        n = len(buf) // step_bytes
        if n:
            x = np.frombuffer(buf[: n * step_bytes], dtype=np.int16).astype(np.float32) / 32768.0
            rms = np.sqrt((x.reshape(n, rate) ** 2).mean(axis=1)) + 1e-6
            vals.extend((20 * np.log10(rms)).tolist())
            buf = buf[n * step_bytes:]
    return np.array(vals, dtype=np.float32)


def loudness_per_sec(path) -> np.ndarray:
    proc = subprocess.Popen([FFMPEG, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000",
                             "-f", "s16le", "-"], stdout=subprocess.PIPE)
    try:
        return pcm_seconds_db(proc.stdout)
    finally:
        proc.wait()


def wav_seconds_db(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    n = len(x) // rate
    if n == 0:
        return np.array([-90.0], dtype=np.float32)
    rms = np.sqrt((x[: n * rate].reshape(n, rate) ** 2).mean(axis=1)) + 1e-6
    return (20 * np.log10(rms)).astype(np.float32)


_ASR = None


def model_path(cfg: dict) -> Path:
    p = Path(os.path.expanduser(cfg["captions"]["model_dir"]))
    return p if p.is_absolute() else ROOT / p


def asr(cfg: dict):
    global _ASR
    if _ASR is None:
        mdir = model_path(cfg)
        if not (mdir / "model.safetensors").exists():
            raise ClipError(f"The caption model isn't downloaded yet ({mdir}). Run: clip check")
        from parakeet_mlx import from_pretrained
        _ASR = from_pretrained(str(mdir))
    return _ASR


def transcribe(cfg: dict, path: Path):
    """Return (words, sentences): words = [{text, start, end}], sentences = [(start, text)]."""
    r = asr(cfg).transcribe(str(path))
    words: list[dict] = []
    sentences: list[tuple[float, str]] = []
    for s in r.sentences:
        sentences.append((float(s.start), s.text.strip()))
        first = True
        for t in s.tokens:
            if first or t.text.startswith(" ") or not words:
                words.append({"text": t.text.strip(), "start": float(t.start), "end": float(t.end)})
                first = False
            else:
                words[-1]["text"] += t.text
                words[-1]["end"] = float(t.end)
    return [w for w in words if w["text"]], sentences


# ───────────────────────────── chat ─────────────────────────────

def msg_weight(text: str) -> float:
    return 1.0 + (1.5 if REACT_RE.search(text) else 0.0) + (3.0 if CLIP_RE.search(text) else 0.0)


def twitch_chat(vod_id: str, a0: float, b0: float, threads: int = 8) -> dict | None:
    offsets = list(range(int(a0), int(b0) + 1, WIN))

    def fetch(off: int):
        body = json.dumps([{
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": {"videoID": vod_id, "contentOffsetSeconds": off},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TWITCH_COMMENTS_HASH}},
        }]).encode()
        for attempt in range(4):
            try:
                req = urllib.request.Request("https://gql.twitch.tv/gql", data=body, headers={
                    "Client-Id": TWITCH_CLIENT_ID, "Content-Type": "application/json", "User-Agent": "clipkit/1.0"})
                with urllib.request.urlopen(req, context=SSL_CTX, timeout=20) as r:
                    d = json.load(r)
                if not d or "errors" in d[0]:
                    return None
                video = (d[0].get("data") or {}).get("video") or {}
                return (video.get("comments") or {}).get("edges") or []
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
                time.sleep(1.5 * (attempt + 1))
        return []

    msgs: dict = {}
    spans: list[tuple[float, float]] = []
    failures = 0
    step(f"Reading chat replay ({len(offsets)} samples)...")
    next_report = 0.25
    with cf.ThreadPoolExecutor(threads) as ex:
        for i, edges in enumerate(ex.map(fetch, offsets), 1):
            if edges is None:
                failures += 1
                continue
            ts = []
            for e in edges:
                n = e.get("node") or {}
                t = n.get("contentOffsetSeconds")
                if t is None:
                    continue
                text = "".join((f or {}).get("text") or "" for f in ((n.get("message") or {}).get("fragments") or []))
                msgs[n.get("id") or f"{t}:{text}"] = (float(t), text)
                ts.append(float(t))
            if ts:
                spans.append((min(ts), max(ts)))
            if i / len(offsets) >= next_report:
                say(f"    {int(next_report * 100)}% ({len(msgs)} messages so far)")
                next_report += 0.25
    if failures > len(offsets) * 0.5 or not msgs:
        warn("Chat replay isn't available for this stream; using volume only.")
        return None
    nsec = int(math.ceil((b0 - a0) / WIN)) * WIN
    cover = np.zeros(nsec, dtype=np.float32)
    for s, e in spans:
        lo, hi = max(0, int(s - a0)), min(nsec, int(e - a0) + 1)
        if hi > lo:
            cover[lo:hi] = 1
    items = sorted(msgs.values())
    return {"times": np.array([t for t, _ in items]), "texts": [x for _, x in items],
            "weights": np.array([msg_weight(x) for _, x in items]), "cover": cover}


def youtube_chat(src: dict, work: Path, a0: float, b0: float) -> dict | None:
    step("Reading chat replay...")
    run([*YTDLP, "--no-warnings", "-q", "--skip-download", "--write-subs", "--sub-langs", "live_chat",
         "-o", str(work / "chat"), src["url"]], check=False)
    path = work / "chat.live_chat.json"
    if not path.exists():
        warn("This video has no chat replay; using volume only.")
        return None
    items = []
    with path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            r = d.get("replayChatItemAction") or {}
            t = int(r.get("videoOffsetTimeMsec", 0)) / 1000
            if not (a0 <= t <= b0):
                continue
            for act in r.get("actions", []):
                m = ((act.get("addChatItemAction") or {}).get("item") or {}).get("liveChatTextMessageRenderer")
                if m:
                    runs = (m.get("message") or {}).get("runs", [])
                    text = "".join(x.get("text") or ((x.get("emoji") or {}).get("shortcuts") or [""])[0] for x in runs)
                    items.append((t, text))
    if len(items) < 50:
        warn("Chat replay is too quiet to use; using volume only.")
        return None
    items.sort()
    return {"times": np.array([t for t, _ in items]), "texts": [x for _, x in items],
            "weights": np.array([msg_weight(x) for _, x in items]), "cover": None}


def chat_rate(chat: dict, a0: float, nwin: int) -> np.ndarray:
    nsec = nwin * WIN
    t = chat["times"] - a0
    keep = (t >= 0) & (t < nsec)
    per_sec = np.bincount(t[keep].astype(int), weights=chat["weights"][keep], minlength=nsec)[:nsec]
    cover = chat["cover"] if chat["cover"] is not None else np.ones(nsec, dtype=np.float32)
    cover = np.pad(cover, (0, max(0, nsec - len(cover))))[:nsec]
    cw = per_sec.reshape(nwin, WIN).sum(1)
    cv = cover.reshape(nwin, WIN).sum(1)
    rate = np.where(cv > 0, cw / np.maximum(cv, 1), np.nan)
    return interp_nan(rate)


def chat_samples(chat: dict | None, p: float, n: int = 6) -> list[str]:
    if not chat:
        return []
    lo, hi = np.searchsorted(chat["times"], [p - 2, p + 25])
    picked, seen = [], set()
    idx = list(range(lo, hi))
    idx.sort(key=lambda i: -chat["weights"][i])
    for i in idx:
        txt = chat["texts"][i].strip()
        k = txt.lower()
        if txt and k not in seen and len(txt) <= 80:
            seen.add(k)
            picked.append(txt[:60])
        if len(picked) >= n:
            break
    return picked


# ───────────────────────────── scoring ─────────────────────────────

def interp_nan(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    ok = ~np.isnan(x)
    if not ok.any():
        return np.zeros_like(x)
    idx = np.arange(len(x))
    return np.interp(idx, idx[ok], x[ok])


def robust_z(x: np.ndarray, half: int = 15) -> np.ndarray:
    """How unusual each value is compared with the ~5 minutes around it."""
    n = len(x)
    z = np.zeros(n)
    for i in range(n):
        seg = x[max(0, i - half): i + half + 1]
        med = np.median(seg)
        scale = 1.4826 * np.median(np.abs(seg - med))
        if scale < 1e-6:
            scale = max(float(np.std(seg)), 1e-3)
        z[i] = (x[i] - med) / scale
    return np.clip(z, -3, 8)


def local_ratio(rate: np.ndarray, k: int, half: int = 15) -> float:
    """Chat speed just after window k, compared with the ~5 minutes around it (same basis as the score)."""
    base = float(np.median(rate[max(0, k - half): k + half + 1]))
    peak = float(rate[k: k + 3].max())
    return peak / base if base > 0 else 0.0


def pick_peaks(score: np.ndarray, n: int, min_gap: int = 9, min_score: float = 0.8) -> list[int]:
    chosen: list[int] = []
    for k in np.argsort(-score):
        if score[k] < min_score:
            break
        if all(abs(int(k) - c) > min_gap for c in chosen):
            chosen.append(int(k))
        if len(chosen) >= n:
            break
    return chosen


def heat_windows(info: dict, a0: float, nwin: int) -> np.ndarray | None:
    hm = info.get("heatmap")
    if not hm:
        return None
    out = np.full(nwin, np.nan)
    for k in range(nwin):
        t = a0 + k * WIN + WIN / 2
        for seg in hm:
            if seg.get("start_time", 0) <= t < seg.get("end_time", 0):
                out[k] = seg.get("value", 0)
                break
    out = interp_nan(out)
    med = np.median(out)
    scale = 1.4826 * np.median(np.abs(out - med)) or 1e-3
    return np.clip((out - med) / scale, -2, 6)


# ───────────────────────────── AI ranking ─────────────────────────────

DOTENV_KEYS = ("opencode_go_key",)  # names accepted in the folder's .env file (any case)


def dotenv_key() -> str | None:
    path = ROOT / ".env"
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        k, sep, v = line.strip().partition("=")
        k = k.strip().lower().removeprefix("export ").strip()
        if sep and k in DOTENV_KEYS:
            v = v.strip().strip('"').strip("'").strip()
            if v:
                return v
    return None


def api_key_with_source(r: dict) -> tuple[str | None, str | None]:
    """The AI key and where it came from: config.yaml's key_from first, then the folder's .env file."""
    src = str(r.get("key_from", "keychain:opencode-go"))
    if src.startswith("keychain:"):
        res = subprocess.run(["security", "find-generic-password", "-s", src.split(":", 1)[1], "-w"],
                             capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip(), "Keychain"
    elif src.startswith("env:") and os.environ.get(src[4:]):
        return os.environ[src[4:]], f"environment variable {src[4:]}"
    key = dotenv_key()
    return (key, ".env file") if key else (None, None)


def api_key(r: dict) -> str | None:
    return api_key_with_source(r)[0]


LIMIT_WORDS = re.compile(r"limit|quota|allowance|exceed|insufficient|credit|balance|billing|too many", re.I)


def chat_completion(r: dict, key: str, messages: list, session: str, max_tokens: int | None = None,
                    timeout: float = 240) -> dict:
    url = r["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": r["model"], "messages": messages, "temperature": 0.3,
            "max_tokens": int(max_tokens or r.get("max_tokens", 4000))}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "clipkit/1.0", "x-opencode-session": session})
    try:
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        ra = (e.headers.get("Retry-After") or "").strip()
        retry = float(ra) if ra.isdigit() else None
        if e.code in (402, 429) or (e.code in (400, 403) and LIMIT_WORDS.search(detail)):
            kind = "limit"
        elif e.code in (401, 403):
            kind = "auth"
        elif e.code in (400, 404, 422):
            kind = "config"
        else:
            kind = "down"
        raise AIError(f"AI request failed ({e.code}): {detail}", kind, retry)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as e:
        raise AIError(f"the AI isn't reachable ({e})", "down")
    if isinstance(data, dict) and data.get("error") and not data.get("choices"):
        msg = json.dumps(data["error"])[:300]
        raise AIError(f"AI request failed: {msg}", "limit" if LIMIT_WORDS.search(msg) else "down")
    return data


# The AI on/off switch. When the AI is unreachable, refuses the key, or the usage limit is reached, every
# command stops calling it (and falls back to working without it) until a pause runs out; the first call
# after that is a test. Pauses grow each time the test fails. A new key or model in config resets it.
AI_STATE = ROOT / "ai_state.json"
PAUSE = {"limit": (3600, 12 * 3600), "down": (300, 2 * 3600)}  # (first pause, longest pause) in seconds


def _ai_fp(r: dict, key: str) -> str:
    return hashlib.sha256(f"{key}|{r.get('model')}|{r.get('base_url')}".encode()).hexdigest()[:16]


def ai_state() -> dict:
    try:
        return json.loads(AI_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_ai_state(d: dict) -> None:
    tmp = AI_STATE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(AI_STATE)


def ai_resume() -> None:
    """Forget any pause, so the next AI call is tried straight away."""
    _save_ai_state({"last_ok": ai_state().get("last_ok")})


def ai_pause(r: dict, key: str | None) -> dict | None:
    """The current pause, or None when the AI may be called."""
    st = ai_state()
    if not st.get("paused"):
        return None
    if key and st.get("fp") and st["fp"] != _ai_fp(r, key):
        ai_resume()  # the key, model or address changed: try again
        return None
    if st.get("until") and time.time() >= st["until"]:
        return None  # the pause is over; the next call tests whether the AI is back
    return st


def pause_text(st: dict) -> str:
    until = st.get("until")
    when = ""
    if until:
        t = dt.datetime.fromtimestamp(until)
        when = t.strftime("%H:%M") if t.date() == dt.date.today() else t.strftime("%d %b %H:%M")
    return {
        "limit": f"AI usage limit reached; paused until {when}",
        "down": f"the AI isn't reachable; paused until {when}",
        "auth": "the AI refused the key; paused until you change the key (or press Try AI now)",
        "config": "the AI model or address wasn't found; paused until you change config.yaml (or press Try AI now)",
    }.get(st.get("kind"), "AI paused")


def _ai_failed(r: dict, key: str, err: AIError) -> None:
    st = ai_state()
    strikes = st.get("strikes", 0) + 1 if st.get("kind") == err.kind else 1
    until = None
    if err.kind in PAUSE:
        first, longest = PAUSE[err.kind]
        wait = err.retry_after or min(first * 2 ** (strikes - 1), longest)
        until = time.time() + min(wait, 24 * 3600)
    _save_ai_state({"paused": True, "kind": err.kind, "strikes": strikes, "since": time.time(), "until": until,
                    "fp": _ai_fp(r, key), "reason": str(err)[:300], "last_ok": st.get("last_ok")})


def ai_request(cfg: dict, messages: list, max_tokens: int | None = None, timeout: float = 240) -> dict:
    """One AI call through the on/off switch. Raises AISkip while the AI is off or paused."""
    r = cfg["rank"]
    if str(r.get("provider", "none")).lower() == "none":
        raise AISkip("AI is off (config.yaml → rank.provider)")
    key = api_key(r)
    if not key:
        raise AISkip("no AI key found (see README: 'Add your OpenCode Go key')")
    if p := ai_pause(r, key):
        raise AISkip(pause_text(p))
    try:
        resp = chat_completion(r, key, messages, session=str(uuid.uuid4()), max_tokens=max_tokens, timeout=timeout)
    except AIError as e:
        _ai_failed(r, key, e)
        raise
    if ai_state().get("paused") or ai_state().get("strikes"):
        say("    The AI is back; using it again.")
    _save_ai_state({"last_ok": time.time()})
    return resp


def ai_status(cfg: dict) -> dict:
    """For the page: is the AI on, paused (and why), or off."""
    r = cfg["rank"]
    provider = str(r.get("provider", "none")).lower()
    key, where = api_key_with_source(r) if provider != "none" else (None, None)
    p = ai_pause(r, key) if key else None
    return {"on": bool(key) and not p, "provider": provider, "model": r.get("model", ""), "key": where,
            "paused": pause_text(p) if p else "", "pause_kind": (p or {}).get("kind", ""),
            "last_ok": ai_state().get("last_ok")}


def parse_json_block(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ClipError("the AI didn't reply with JSON")
    return json.loads(text[a: b + 1])


def rank_with_ai(cfg: dict, cands: list[dict], meta: dict, top: int):
    blocks = []
    for c in cands:
        lines = "\n".join(f"[{fmt_time(t)}] {txt}" for t, txt in c["sentences"]) or "(no speech)"
        chat = ("\nchat said: " + " | ".join(c["chat_samples"])) if c["chat_samples"] else ""
        sig = f"volume +{c['loud_db']:.0f} dB" + (f", chat {c['chat_x']:.1f}x normal" if c["chat_x"] else "")
        blocks.append(f"### Candidate {c['id']} (peak {fmt_time(c['peak'])}; transcript range "
                      f"{fmt_time(c['seg_start'])}-{fmt_time(c['seg_end'])}; {sig}){chat}\n{lines}")
    user = f"Streamer: {meta['streamer']}\nStream title: {meta['title']}\n\n" + "\n\n".join(blocks)
    messages = [{"role": "system", "content": SYSTEM_PROMPT.replace("{top}", str(top))},
                {"role": "user", "content": user}]
    try:
        resp = ai_request(cfg, messages)
    except AISkip as e:
        return None, str(e)
    content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    data = parse_json_block(content)
    return {"moments": data.get("moments") or [], "usage": resp.get("usage") or {},
            "allowance": (resp.get("opencode") or {}).get("allowance")}, None


def fit_moment(c: dict, s: float, e: float) -> tuple[float, float]:
    s = max(c["seg_start"], min(s, c["seg_end"] - 10))
    e = min(c["seg_end"], max(e, s + 10))
    if e - s > 60:
        e = s + 45
    return s, e


# ───────────────────────────── findmoments ─────────────────────────────

def section_wavs(inp: dict, cands: list[dict], work: Path, threads: int) -> None:
    def grab(c):
        # named by time, so a re-run over a different range never reuses the wrong piece
        out = work / f"cand-{int(c['seg_start'])}-{int(c['seg_end'])}.wav"
        if not out.exists():
            run([FFMPEG, "-v", "error", "-y", *ff_input(inp, c["seg_start"] - c["_offset"],
                                                         c["seg_end"] - c["seg_start"]),
                 "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out])
        c["wav"] = out
    with cf.ThreadPoolExecutor(threads) as ex:
        list(ex.map(grab, cands))


def fetch_audio(src: dict, work: Path, rng: tuple[float, float] | None) -> Path:
    """Download the stream's audio with yt-dlp's own downloader (fast, resumable); reuse it if already here."""
    name = "audio" if rng is None else f"audio-{int(rng[0])}-{int(rng[1])}"

    def finished():
        return [p for p in sorted(work.glob(f"{name}.*")) if ".part" not in p.name and not p.name.endswith(".ytdl")]

    if finished():
        return finished()[0]
    # YouTube: plain (not "DRC" volume-squashed) low-bitrate audio, so loud moments still stand out
    fmt = "139/249/140/ba[format_id!*=drc]/ba" if src["kind"] == "youtube" else "ba/worst"
    cmd = [*YTDLP, "--no-warnings", "-q", "--progress", "-N", "8", "-f", fmt, "-o", str(work / f"{name}.%(ext)s")]
    if rng:
        cmd += ["--download-sections", f"*{rng[0]:.0f}-{rng[1]:.0f}", "--force-keyframes-at-cuts"]
    step("Downloading the audio" + (f" ({fmt_time(rng[1] - rng[0])})" if rng else "") + "...")
    run(cmd + [src["url"]], live=True)
    if not finished():
        raise ClipError("The audio download didn't produce a file.")
    return finished()[0]


def resolve_streamer_name(explicit: str | None, info: dict) -> str:
    if explicit:
        return explicit.lower()
    ids = {str(info.get(k) or "").lstrip("@").lower() for k in ("uploader_id", "channel", "uploader", "channel_id")}
    for name, s in load_streamers().items():
        if all_handles(s) & ids:
            return name
    guess = next((str(info.get(k)).lstrip("@").lower() for k in ("uploader_id", "uploader") if info.get(k)), "")
    return slug(guess) if guess else "unknown"


def cmd_findmoments(a) -> None:
    cfg = load_config()
    clean_inbox(cfg)
    src = source_of(a.link)
    if src["kind"] in ("twitch-clip", "kick-clip"):
        raise ClipError("That's a single clip, not a full stream. Turn it into a vertical clip with makeclip.")
    st = load_state()
    if src["key"] in st["streams"] and not a.again:
        prev = st["streams"][src["key"]]
        say(f"Already analysed on {prev['date']}: {prev['moments_file']}\n  (add --again to analyse it again)")
        report(key=src["key"], already=True)
        return
    work = ROOT / "inbox" / src["key"]
    work.mkdir(parents=True, exist_ok=True)

    step("Looking up the stream...")
    info = probe(src, "ba/worst")
    if info.get("is_live") or info.get("live_status") == "is_live":
        raise ClipError("This stream is still live. Wait for the VOD, or record it with: clip record <streamer>")
    duration = float(info.get("duration") or 0)
    a0 = parse_time(a.from_) if a.from_ else 0.0
    b0 = min(parse_time(a.to), duration) if a.to else duration
    if b0 - a0 < 120:
        raise ClipError("The part to analyse is shorter than 2 minutes. Check --from/--to.")
    streamer = resolve_streamer_name(a.streamer, info)
    s_conf = load_streamers().get(streamer)
    if s_conf and not permission(s_conf)[0]:
        warn(f"{s_conf.get('display', streamer)} isn't marked as allowing clips yet; makeclip will only do practice clips.")
    nwin = int(math.ceil((b0 - a0) / WIN))
    say(f"  {(info.get('title') or '')[:70]}  ({fmt_time(a0)}–{fmt_time(b0)})")

    chat = None
    if src["kind"] == "twitch":
        chat = twitch_chat(src["id"], a0, b0)
    elif src["kind"] == "youtube":
        chat = youtube_chat(src, work, a0, b0)
    heat = heat_windows(info, a0, nwin) if src["kind"] == "youtube" else None

    n_cands = int(a.candidates or cfg["findmoments"]["candidates"])
    top = int(a.top or cfg["findmoments"]["top"])
    rate = chat_rate(chat, a0, nwin) if chat else None
    chat_z = None
    if rate is not None:
        z = robust_z(np.log1p(rate))
        chat_z = np.array([z[k: k + 3].max() for k in range(nwin)])  # chat reacts 0-30 s after the moment
    quick = chat is not None and not a.full_audio
    audio_in = info["_input"][0]
    src_offset = 0.0  # sections are cut from a source whose time 0 is the stream's start
    local_audio = None
    if src["kind"] == "youtube":
        # YouTube throttles direct reads to ~2x playback speed. yt-dlp's chunked downloader is fast and
        # the audio is small (~22 MB per hour), so fetch it once and cut everything from the local copy.
        local_audio = fetch_audio(src, work, None)
        audio_in = {"url": str(local_audio), "headers": None}

    loud = loud_z = None
    if not quick:
        if src["kind"] == "file":
            audio_path, offset = Path(src["path"]), 0.0
        elif local_audio is not None:
            audio_path, offset = local_audio, 0.0
        else:  # Twitch / Kick serve HLS, where downloading just the needed range is fast
            whole = a0 <= 0 and b0 >= duration - 1
            audio_path = fetch_audio(src, work, None if whole else (a0, b0))
            offset = 0.0 if whole else a0
        step("Measuring volume...")
        per_sec = loudness_per_sec(audio_path)
        start_sec = int(a0 - offset)
        per_sec = per_sec[start_sec: start_sec + nwin * WIN]
        per_sec = np.pad(per_sec, (0, max(0, nwin * WIN - len(per_sec))), constant_values=-90)
        loud = per_sec.reshape(nwin, WIN)
        loud_win = np.percentile(loud, 90, axis=1)
        loud_z = robust_z(loud_win)
        audio_in = {"url": str(audio_path), "headers": None}
        src_offset = offset

    score = np.zeros(nwin)
    if chat_z is not None and loud_z is not None:
        score = CHAT_WEIGHT * chat_z + LOUD_WEIGHT * np.clip(loud_z, -2, LOUD_CAP)
    elif chat_z is not None:
        score = chat_z.copy()
    elif loud_z is not None:
        score = loud_z.copy()
    if heat is not None:
        score = score + 0.5 * heat

    pool = pick_peaks(score, int(n_cands * 1.6) if quick else n_cands)
    if not pool:
        raise ClipError("Nothing stood out in this part of the stream (no chat or volume spikes).")
    cands = []
    for i, k in enumerate(sorted(pool), 1):
        if loud is not None:
            p = a0 + k * WIN + int(np.argmax(loud[k]))
        else:
            p = a0 + k * WIN + WIN / 2
        c = {"id": i, "k": k, "peak": p, "seg_start": max(a0, p - 75), "seg_end": min(b0, p + 30),
             "score": float(score[k]), "chat_z": float(chat_z[k]) if chat_z is not None else 0.0,
             "chat_x": local_ratio(rate, k) if rate is not None else 0.0,
             "loud_db": float(loud_win[k] - np.median(loud_win)) if loud is not None else 0.0,
             "heat": float(heat[k]) if heat is not None else 0.0, "_offset": src_offset}
        cands.append(c)

    step(f"Grabbing audio around {len(cands)} spikes...")
    section_wavs(audio_in, cands, work, threads=4 if quick else 6)

    if quick:  # measure volume inside each section now that we have it
        for c in cands:
            db = wav_seconds_db(c["wav"])
            rel = int(c["peak"] - c["seg_start"])
            around = db[max(0, rel - 5): rel + 15]
            med = float(np.median(db))
            mad = 1.4826 * float(np.median(np.abs(db - med))) or 1.0
            lz = float(np.clip((np.percentile(around, 90) - med) / mad, -3, 8)) if len(around) else 0.0
            c["loud_db"] = float(np.percentile(around, 90) - med) if len(around) else 0.0
            c["score"] = CHAT_WEIGHT * c["chat_z"] + LOUD_WEIGHT * min(lz, LOUD_CAP) + 0.5 * c["heat"]
            if len(around):  # move the peak to the loudest second near it
                c["peak"] = c["seg_start"] + max(0, rel - 5) + int(np.argmax(around))

    step(f"Transcribing {len(cands)} moments (on your Mac)...")
    for c in cands:
        words, sents = transcribe(cfg, c["wav"])
        c["sentences"] = [(c["seg_start"] + t, txt) for t, txt in sents]
        c["words_near"] = sum(1 for w in words if -20 <= c["seg_start"] + w["start"] - c["peak"] <= 15)
        c["chat_samples"] = chat_samples(chat, c["peak"])
    before = len(cands)
    cands = [c for c in cands if c["words_near"] >= 4 or c["chat_z"] >= 1.5]  # loud, but nobody talking
    if before - len(cands):
        say(f"    dropped {before - len(cands)} loud moments where nobody was talking")
    cands = sorted(cands, key=lambda c: -c["score"])[:n_cands]
    for i, c in enumerate(sorted(cands, key=lambda c: c["peak"]), 1):
        c["id"] = i
    if not cands:
        raise ClipError("No usable moments found in this part of the stream.")

    meta = {"streamer": (s_conf or {}).get("display", streamer), "title": info.get("title", "")}
    step("Ranking...")
    ai = None
    try:
        ai, why_not = rank_with_ai(cfg, cands, meta, top)
    except (ClipError, json.JSONDecodeError, KeyError, ValueError) as e:
        why_not = f"AI ranking failed ({e}); ranked by chat and volume instead"
        if isinstance(e, AIError) and (p := ai_state()).get("paused"):
            why_not += f". {pause_text(p).capitalize()}"
    by_id = {c["id"]: c for c in cands}
    moments: list[dict] = []
    if ai:
        for m in ai["moments"]:
            try:
                c = by_id[int(m.get("id"))]
            except (KeyError, TypeError, ValueError):
                continue
            try:
                s, e = parse_time(m.get("start")), parse_time(m.get("end"))
            except ValueError:
                s, e = c["peak"] - 20, c["peak"] + 12
            s, e = fit_moment(c, s, e)
            moments.append({"cand": c, "start": s, "end": e, "score": m.get("score"),
                            "title": str(m.get("title") or "").strip(), "why": str(m.get("why") or "").strip(),
                            "hook": str(m.get("hook") or "").strip(), "spoiler": bool(m.get("spoiler"))})
        moments = moments[:top]
    if not moments:
        if ai is not None:
            why_not = "the AI didn't return usable moments; ranked by chat and volume instead"
        for c in sorted(cands, key=lambda c: -c["score"])[:top]:
            s, e = fit_moment(c, c["peak"] - 20, c["peak"] + 12)
            first = next((txt for t, txt in c["sentences"] if t >= c["peak"] - 25), "")
            moments.append({"cand": c, "start": s, "end": e, "score": None, "title": first[:60],
                            "why": "", "hook": "", "spoiler": False})
        ai = None

    # write the moments file
    day = dt.date.today().isoformat()
    out_dir = ROOT / "moments" / streamer
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{day}-{src['key']}.md"
    made = [x for x in st["clips"] if x.get("key") == src["key"]]
    usage_line = "Ranked by chat and volume only" if chat else "Ranked by volume only (no chat replay for this video)"
    if ai:
        u = ai["usage"]
        pin, pout = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        price = cfg["rank"].get("price_per_million") or {}
        cost = pin / 1e6 * float(price.get("input", 0)) + pout / 1e6 * float(price.get("output", 0))
        usage_line = f"Ranked by {cfg['rank']['model']}: {pin:,} tokens in, {pout:,} out (≈ ${cost:.4f})"
    elif why_not:
        usage_line += f" ({why_not})"
    lines = [f"# Moments: {meta['streamer']}", "",
             f"**Stream:** {info.get('title', '')}  ",
             f"**Link:** {src['url']}  ",
             f"**Analysed:** {fmt_time(a0)}–{fmt_time(b0)} on {day}  ",
             f"**{usage_line}**", "",
             "Watch each moment with its link. For the ones you want, copy the makeclip line into Terminal.", ""]
    for i, m in enumerate(moments, 1):
        c = m["cand"]
        title = m["title"] or f"Moment at {fmt_time(c['peak'])}"
        score = f"  ·  {m['score']}/10" if m["score"] else ""
        lines.append(f"## {i}. {title}{score}")
        if m["why"]:
            lines.append(m["why"])
        sig = [f"volume +{c['loud_db']:.0f} dB"]
        if c["chat_x"]:
            sig.insert(0, f"chat {c['chat_x']:.1f}× normal")
        lines += ["", f"- **Watch:** {link_at(src, m['start'])}",
                  f"- **Clip:** {fmt_time(m['start'])} → {fmt_time(m['end'])} ({int(m['end'] - m['start'])} s) · {', '.join(sig)}"]
        if m["hook"]:
            lines.append(f"- **Hook idea:** {m['hook']}")
        if m["spoiler"]:
            lines.append("- ⚠ **Possible GTA 6 story spoiler.** Don't post story moments.")
        if any(x["start"] < m["end"] and m["start"] < x["end"] for x in made):
            lines.append("- Already clipped.")
        if c["chat_samples"]:
            lines.append(f"- **Chat:** {' | '.join(c['chat_samples'][:4])}")
        said = [f"[{fmt_time(t)}] {txt}" for t, txt in c["sentences"] if m["start"] - 2 <= t <= m["end"]][:4]
        if said:
            lines += [""] + [f"> {x}  " for x in said]
        hook = f' --hook "{m["hook"]}"' if m["hook"] else ""
        lines += ["", "```", f'makeclip "{src["url"]}" {fmt_time(m["start"])} {fmt_time(m["end"])} {streamer}{hook}', "```", ""]
    out.write_text("\n".join(lines))
    # the same moments as data, for the Clipping page
    data = {"version": 1, "key": src["key"], "kind": src["kind"], "id": src.get("id", ""), "url": src["url"],
            "streamer": streamer, "display": meta["streamer"], "title": info.get("title", ""), "day": day,
            "range": [a0, b0], "duration": duration, "ranking": usage_line, "ai": bool(ai), "chat": chat is not None,
            "moments": [{"n": i, "start": m["start"], "end": m["end"], "peak": c["peak"],
                         "seg_start": c["seg_start"], "seg_end": c["seg_end"],
                         "title": m["title"] or f"Moment at {fmt_time(c['peak'])}", "why": m["why"], "hook": m["hook"],
                         "score": m["score"], "spoiler": m["spoiler"], "chat_x": c["chat_x"], "loud_db": c["loud_db"],
                         "chat": c["chat_samples"], "said": [[t, txt] for t, txt in c["sentences"]],
                         "link": link_at(src, m["start"])}
                        for i, m in enumerate(moments, 1) for c in [m["cand"]]]}
    out.with_suffix(".json").write_text(json.dumps(data, indent=1, ensure_ascii=False, default=float))

    st["streams"][src["key"]] = {
        "date": day, "streamer": streamer, "title": info.get("title", ""), "url": src["url"],
        "range": [a0, b0], "moments_file": str(out),
        "moments": [{"start": m["start"], "end": m["end"], "title": m["title"], "hook": m["hook"]} for m in moments]}
    save_state(st)

    say("")
    say(f"✓ {len(moments)} moments → {out}")
    for i, m in enumerate(moments[:5], 1):
        say(f"   {i}. {fmt_time(m['start'])}  {(m['title'] or '')[:60]}")
    say(f"   {usage_line}")
    report(key=src["key"], moments_json=str(out.with_suffix(".json")))


# ───────────────────────────── captions ─────────────────────────────

def clean_word(text: str) -> str:
    w = re.sub(r"[{}\\]", "", text).upper()
    return re.sub(r"[^\w'?!%$#&\-]", "", w)


def group_positions(words: list[dict], ccfg: dict) -> list[list[int]]:
    """Split words (clip time) into the 1-3 word caption lines shown on screen; returns positions in words."""
    max_words, max_chars = int(ccfg["max_words"]), int(ccfg["max_chars"])
    groups: list[list[int]] = []
    cur: list[int] = []
    for i, w in enumerate(words):
        t = clean_word(w["text"])
        if not t:
            continue
        if cur:
            last = words[cur[-1]]
            if (len(cur) >= max_words or w["start"] - last["end"] > 0.35
                    or len(" ".join(clean_word(words[j]["text"]) for j in cur)) + 1 + len(t) > max_chars
                    or last["text"][-1:] in ".?!,"):
                groups.append(cur)
                cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    return groups


def map_words(words: list[dict], keep) -> list[tuple[int, dict]]:
    """Words in stream-piece time → (index, word in clip time), dropping words inside cut silences."""
    out = []
    for i, w in enumerate(words):
        s2, e2 = remap(w["start"], keep), remap(w["end"], keep)
        if s2 is None:
            continue
        out.append((i, {**w, "start": s2, "end": e2 if e2 is not None and e2 > s2 else s2 + 0.25}))
    return out


def caption_lines(words: list[dict], keep, ccfg: dict):
    """The on-screen caption lines: (mapped words, groups, lines). Each line knows which words it holds."""
    mapped = map_words(words, keep)
    groups = group_positions([w for _, w in mapped], ccfg)
    lines = [{"i": gi, "idx": [mapped[p][0] for p in g], "start": round(mapped[g[0]][1]["start"], 2),
              "end": round(mapped[g[-1]][1]["end"], 2), "text": " ".join(words[mapped[p][0]]["text"] for p in g)}
             for gi, g in enumerate(groups)]
    return mapped, groups, lines


def respace(old: list[dict], tokens: list[str]) -> list[dict]:
    """New words for the time the old ones took: same timing if the count matches, else shared out by length."""
    if not tokens or not old:
        return []
    if len(tokens) == len(old):
        return [{**w, "text": t} for w, t in zip(old, tokens)]
    s, e = old[0]["start"], old[-1]["end"]
    weights = [max(1, len(t)) for t in tokens]
    total, t0, out = sum(weights), s, []
    for t, wgt in zip(tokens, weights):
        d = (e - s) * wgt / total
        out.append({"text": t, "start": t0, "end": t0 + d})
        t0 += d
    return out


def apply_line_edits(words: list[dict], lines: list[dict], edits: dict) -> list[dict]:
    """edits = {line number: new text}. An empty text removes that line's words."""
    out = list(words)
    for li in sorted(edits, key=lambda k: -lines[k]["idx"][0]):
        idx = lines[li]["idx"]
        a, b = idx[0], idx[-1] + 1
        out[a:b] = respace(out[a:b], str(edits[li]).split())
    return out


def norm_word(t: str) -> str:
    return re.sub(r"[^\w']", "", t.lower())


def apply_rules(words: list[dict], rules: list[dict]) -> tuple[list[dict], int]:
    """Word fixes like 'cold warmer' → 'Cole Palmer', matched across words, ignoring case and punctuation."""
    out, n = list(words), 0
    for r in rules or []:
        pat = [p for p in (norm_word(t) for t in str(r.get("from", "")).split()) if p]
        rep = str(r.get("to", "")).split()
        if not pat or not rep:
            continue
        i = 0
        while i <= len(out) - len(pat):
            if [norm_word(w["text"]) for w in out[i:i + len(pat)]] == pat:
                tail = re.search(r"[.,!?]+$", out[i + len(pat) - 1]["text"])
                new = respace(out[i:i + len(pat)], list(rep))
                if tail and new and not re.search(r"[.,!?]$", new[-1]["text"]):
                    new[-1] = {**new[-1], "text": new[-1]["text"] + tail[0]}
                out[i:i + len(pat)] = new
                i += len(new)
                n += 1
            else:
                i += 1
    return out, n


FIXES = ROOT / "fixes.yaml"
FIXES_HEAD = ("# Words the caption model gets wrong, fixed automatically in every new clip.\n"
              "# 'all' applies to every streamer; 'streamers' only to that streamer. Edit freely, or use\n"
              "# 'Replace everywhere' → 'Always' in the Clipping page's caption editor.\n")


def load_fixes() -> dict:
    try:
        d = yaml.safe_load(FIXES.read_text()) or {}
    except (OSError, yaml.YAMLError):
        d = {}
    return {"all": list(d.get("all") or []), "streamers": dict(d.get("streamers") or {})}


def rules_for(name: str) -> list[dict]:
    f = load_fixes()
    return [r for r in f["all"] + list(f["streamers"].get(name) or []) if isinstance(r, dict)]


def save_fixes(d: dict) -> None:
    tmp = FIXES.with_suffix(".tmp")
    tmp.write_text(FIXES_HEAD + yaml.safe_dump({"all": d["all"], "streamers": d["streamers"]},
                                               allow_unicode=True, sort_keys=False, width=200))
    tmp.replace(FIXES)


def build_ass(words: list[dict], ccfg: dict, total: float, cap_y: float, hook: str | None, hook_y: float,
              groups_pos: list[list[int]] | None = None) -> str:
    white, hi = ass_color(ccfg["color"]), ass_color(ccfg["highlight"])
    font, size = ccfg["font"], int(ccfg["size"])
    if groups_pos is None:
        groups_pos = group_positions(words, ccfg)
    groups = [[{**words[i], "t": clean_word(words[i]["text"])} for i in g] for g in groups_pos]

    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUT_W}", f"PlayResY: {OUT_H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{size},{white},{white},&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1",
        f"Style: Hook,{font},60,&H00000000,&H00000000,&H00FFFFFF,&H00000000,0,0,0,0,100,100,0,0,3,16,0,5,90,90,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ev = []
    if hook:
        h = re.sub(r"[{}\\]", "", hook).strip()
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(total)},Hook,,0,0,0,,{{\\pos(540,{int(hook_y)})}}{h}")
    pos = f"\\pos(540,{int(cap_y)})"
    for gi, g in enumerate(groups):
        g_end = g[-1]["end"] + 0.2
        if gi + 1 < len(groups):
            g_end = min(g_end, groups[gi + 1][0]["start"])
        g_end = min(g_end, total)
        for wi, w in enumerate(g):
            s = w["start"] if wi else g[0]["start"]
            e = g[wi + 1]["start"] if wi + 1 < len(g) else g_end
            if e - s < 0.04:
                continue
            text = " ".join(f"{{\\c{hi}}}{x['t']}{{\\c{white}}}" if xi == wi else x["t"] for xi, x in enumerate(g))
            pop = "\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)" if wi == 0 else ""
            ev.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{{{pos}{pop}}}{text}")
    return "\n".join(head + ev) + "\n"


# ───────────────────────────── layout + render ─────────────────────────────

def load_layout(name: str) -> dict:
    p = ROOT / "layouts" / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {"mode": "full"}


def layout_filter(lay: dict, W: int, H: int, src_label: str = "[vc]", out_label: str = "[lay]"):
    """Return (filter text, caption/hook positions) for a 1080x1920 frame."""
    if lay.get("mode") == "camera_top" and lay.get("cam"):
        fx, fy, fw, fh = lay["cam"]
        cx, cy, cw, ch = fx * W, fy * H, fw * W, fh * H
        cam_h = OUT_W * ch / cw
        if cam_h > 840:  # very tall camera box: trim it so the game keeps most of the screen
            new_ch = cw * 840 / OUT_W
            cy += (ch - new_ch) / 2
            ch, cam_h = new_ch, 840
        cam_h = even(cam_h)
        game_h = OUT_H - cam_h
        gw, gh = H * OUT_W / game_h, float(H)
        if gw > W:
            gw, gh = float(W), W * game_h / OUT_W
        gx = min(max(float(lay.get("game_center", 0.5)) * W - gw / 2, 0), W - gw)
        gy = (H - gh) / 2
        f = (f"{src_label}split=2[c][g];"
             f"[c]crop={even(cw)}:{even(ch)}:{even(cx)}:{even(cy)},scale={OUT_W}:{cam_h}:flags=lanczos,setsar=1[cam];"
             f"[g]crop={even(gw)}:{even(gh)}:{even(gx)}:{even(gy)},scale={OUT_W}:{game_h}:flags=lanczos,setsar=1[game];"
             f"[cam][game]vstack=inputs=2{out_label}")
        return f, {"cap_y": min(1380, cam_h + game_h * 0.5), "hook_y": cam_h}
    fg_h = even(OUT_W * H / W)
    top = (OUT_H - fg_h) / 2
    f = (f"{src_label}split=2[b][f];"
         f"[b]scale=-2:480,crop=270:480,boxblur=10:2,scale={OUT_W}:{OUT_H},eq=brightness=-0.06,setsar=1[bg];"
         f"[f]scale={OUT_W}:-2:flags=lanczos,setsar=1[fg];"
         f"[bg][fg]overlay=(W-w)/2:(H-h)/2{out_label}")
    return f, {"cap_y": min(1450, top + fg_h + 150), "hook_y": max(200, top - 120)}


def silences(path: Path) -> list[tuple[float, float | None]]:
    res = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
                          "silencedetect=noise=-38dB:d=0.8", "-f", "null", "-"], capture_output=True, text=True)
    out, s = [], None
    for line in res.stderr.splitlines():
        if m := re.search(r"silence_start: (-?[\d.]+)", line):
            s = max(0.0, float(m[1]))
        elif (m := re.search(r"silence_end: ([\d.]+)", line)) and s is not None:
            out.append((s, float(m[1])))
            s = None
    if s is not None:
        out.append((s, None))
    return out


def keep_segments(dur: float, sil, pad: float = 0.25):
    cuts = []
    for s, e in sil:
        e = dur if e is None else e
        a = 0.0 if s <= 0.05 else s + pad
        b = dur if e >= dur - 0.05 else e - pad
        if b - a >= 0.4:
            cuts.append((a, b))
    keep, t = [], 0.0
    for a, b in cuts:
        if a > t:
            keep.append((t, a))
        t = max(t, b)
    if t < dur:
        keep.append((t, dur))
    return keep, sum(b - a for a, b in cuts)


def remap(t: float, keep) -> float | None:
    acc = 0.0
    for a, b in keep:
        if t < a:
            return None
        if t <= b:
            return acc + (t - a)
        acc += b - a
    return None


# ───────────────────────────── your brand: the logo on every clip ─────────────────────────────

BRAND_DIR = ROOT / "brand"
BRAND_FILE = BRAND_DIR / "brand.json"
BRAND_SIZES = {"small": 170, "medium": 220, "large": 280}  # logo width on the 1080-wide clip
BRAND_SPOTS = ("top_left", "top_center", "top_right")
BRAND_DEFAULT = {"enabled": True, "position": "top_left", "size": "medium", "opacity": 0.95}
UNBRANDED = ROOT / "inbox" / "_unbranded"  # originals of clips the logo was added to afterwards ('_' dirs are kept)


def brand_settings() -> dict:
    try:
        d = json.loads(BRAND_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    return {**BRAND_DEFAULT, **{k: v for k, v in d.items() if k in BRAND_DEFAULT}}


def save_brand_settings(d: dict) -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    BRAND_FILE.write_text(json.dumps({**brand_settings(), **d}, indent=1))


def current_brand() -> dict | None:
    """Where and how big the logo goes, or None when it's off or there's no logo yet."""
    s, logo = brand_settings(), BRAND_DIR / "logo-cut.png"
    if not s["enabled"] or not logo.exists():
        return None
    w = BRAND_SIZES.get(s["size"], 220)
    x = {"top_left": 36, "top_right": OUT_W - w - 36, "top_center": (OUT_W - w) // 2}.get(s["position"], 36)
    st = logo.stat()
    bid = hashlib.sha256(f"{st.st_size}:{int(st.st_mtime)}:{s['position']}:{w}:{s['opacity']}".encode()).hexdigest()[:12]
    # y 150: below the apps' top bar, above the hook and captions
    return {"path": str(logo), "w": w, "x": x, "y": 150, "opacity": float(s["opacity"]), "id": bid}


def brand_filter(b: dict, src: str, dst: str, idx: int) -> str:
    return (f"[{idx}:v]scale={b['w']}:-2:flags=lanczos,format=rgba,colorchannelmixer=aa={b['opacity']:.2f}[logo];"
            f"{src}[logo]overlay={b['x']}:{b['y']}:format=auto{dst}")


def prepare_logo(src: Path) -> Path:
    """Cut the logo out of its plain background like a sticker, keeping a dark outline round it."""
    from scipy import ndimage
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BRAND_DIR / "_upload.png"
    run([FFMPEG, "-v", "error", "-y", "-i", src, "-frames:v", "1", "-vf", "scale='min(1400,iw)':-2", tmp])
    mi = media_info(tmp)
    w, h = mi["w"], mi["h"]
    px = np.frombuffer(subprocess.run([FFMPEG, "-v", "error", "-i", str(tmp), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                                      capture_output=True, check=True).stdout, np.uint8).reshape(h, w, 4).astype(np.float32)
    rgb, a0 = px[..., :3], px[..., 3] / 255
    if a0.min() < 0.98:  # it already has a see-through background
        alpha = a0
    else:
        k = 8
        corners = np.concatenate([rgb[:k, :k], rgb[:k, -k:], rgb[-k:, :k], rgb[-k:, -k:]]).reshape(-1, 3)
        bg = np.median(corners, axis=0)  # the background colour (usually black or white)
        content = ndimage.binary_opening(np.abs(rgb - bg).max(axis=2) > 45, iterations=1)
        dist = ndimage.distance_transform_edt(~content)
        alpha = ndimage.gaussian_filter(np.clip((18 - dist) / 4, 0, 1), 1.0)
    ys, xs = np.where(alpha > 0.02)
    if not len(ys):
        raise ClipError("Couldn't find the logo in that picture.")
    rgba = np.dstack([rgb, alpha * 255]).clip(0, 255).astype(np.uint8)[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    out = BRAND_DIR / "logo-cut.png"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{rgba.shape[1]}x{rgba.shape[0]}",
                    "-i", "-", str(out)], input=rgba.tobytes(), check=True)
    tmp.replace(BRAND_DIR / "logo.png")
    return out


def clip_brand(mp4: Path) -> str:
    """Which logo setting a finished clip has (''= none)."""
    js = mp4.with_suffix(".json")
    if js.exists():
        try:
            return json.loads(js.read_text()).get("brand", "")
        except json.JSONDecodeError:
            return ""
    idx = UNBRANDED / "index.json"
    return (json.loads(idx.read_text()) if idx.exists() else {}).get(str(mp4.relative_to(ROOT)), "")


def forget_unbranded(mp4: Path) -> None:
    """A clip was made again under the same name: its old logo-free copy no longer applies."""
    key = str(mp4.relative_to(ROOT))
    (UNBRANDED / key.replace("/", "__")).unlink(missing_ok=True)
    idx = UNBRANDED / "index.json"
    if idx.exists():
        d = json.loads(idx.read_text())
        if d.pop(key, None) is not None:
            idx.write_text(json.dumps(d, indent=1))


def brand_finished(mp4: Path, b: dict | None) -> None:
    """Put the logo on (or take it off) a finished clip, working from its unbranded original."""
    UNBRANDED.mkdir(parents=True, exist_ok=True)
    orig = UNBRANDED / str(mp4.relative_to(ROOT)).replace("/", "__")
    if not orig.exists():
        if clip_brand(mp4):
            raise ClipError("its original without a logo is gone; make it again")
        shutil.copy2(mp4, orig)
    tmp = mp4.with_name(mp4.stem + ".making.mp4")
    if b:
        run([FFMPEG, "-v", "error", "-y", "-i", orig, "-i", b["path"], "-filter_complex", brand_filter(b, "[0:v]", "[v]", 1),
             "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-profile:v", "high",
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", tmp])
    else:
        shutil.copy2(orig, tmp)
    tmp.replace(mp4)


def render(work: Path, raw: Path, keep, lay_f: str, ass_name: str, out: Path, fps: float, dur: float,
           brand: dict | None = None) -> None:
    parts = []
    if len(keep) == 1 and keep[0][0] < 0.01 and abs(keep[0][1] - dur) < 0.05:
        parts.append("[0:v]null[vc];[0:a]anull[ac]")
    else:
        labels = []
        for i, (a, b) in enumerate(keep):
            parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}];"
                         f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
            labels.append(f"[v{i}][a{i}]")
        parts.append(f"{''.join(labels)}concat=n={len(keep)}:v=1:a=1[vc][ac]")
    parts.append(lay_f)
    base, logo_in = "[lay]", []
    if brand:  # the logo goes under the captions and hook, so they always stay readable
        parts.append(brand_filter(brand, "[lay]", "[layb]", 1))
        base, logo_in = "[layb]", ["-i", brand["path"]]
    fps_f = ",fps=60" if fps > 61 else ""
    parts.append(f"{base}ass=filename={ass_name}:fontsdir={FONTS_DIR}{fps_f},format=yuv420p[vout]")
    parts.append("[ac]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
    run([FFMPEG, "-v", "error", "-y", "-i", raw.name, *logo_in, "-filter_complex", ";".join(parts),
         "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-profile:v", "high", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], cwd=work)


def grab_section(src: dict, start: float, end: float, out: Path) -> dict:
    if src["kind"] == "youtube":
        # YouTube refuses direct ffmpeg reads (403 Forbidden); yt-dlp's section download handles it.
        for p in out.parent.glob("dl.*"):
            p.unlink()
        run([*YTDLP, "--no-warnings", "-q", "--ffmpeg-location", str(FFBIN),
             "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
             "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
             "--downloader-args", "ffmpeg_o:-c:v libx264 -preset veryfast -crf 14 -c:a aac -b:a 256k",
             "--merge-output-format", "mp4", "-o", str(out.parent / "dl.%(ext)s"), src["url"]])
        got = [p for p in out.parent.glob("dl.*") if p.suffix == ".mp4"]
        if not got:
            raise ClipError("The video download didn't produce a file.")
        got[0].replace(out)
        return {}
    info = probe(src, "bv*[height<=1080]+ba/b[height<=1080]/b")
    inputs = info["_input"]
    if not inputs:
        raise ClipError("Couldn't get a video link for that stream.")
    cmd = [FFMPEG, "-v", "error", "-y"]
    for inp in inputs:
        cmd += ff_input(inp, start, end - start)
    cmd += ["-map", "0:v:0", "-map", "1:a:0" if len(inputs) > 1 else "0:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", str(out)]
    run(cmd)
    return info


LOG_COLUMNS = ["made_on", "clip_file", "streamer", "source_link", "start", "end", "title",
               "youtube_url", "instagram_url", "views_24h", "views_7d", "avg_pct_watched",
               "viewed_vs_swiped", "followers_gained", "shares", "notes"]


CHECKLIST = [
    "Watched it on your phone, start to end",
    "Captions match what's said (names, slang)",
    "No copyrighted music (streamer's music or the GTA radio)",
    "No GTA 6 story spoilers or cutscenes",
    "Starts on the action and ends on the payoff",
]


def checklist(hook: bool) -> list[str]:
    return CHECKLIST + ["Hook line at the top" + (" (already burned in)" if hook else
                                                  " (add it in Kdenlive, or remake with a hook)")]


def post_fields(meta: dict) -> tuple[str, str]:
    """(YouTube description, Instagram caption). Clips made before these existed had one shared caption."""
    title, cap = meta.get("title") or "", meta.get("caption") or ""
    yt = meta.get("yt_description")
    ig = meta.get("ig_caption")
    if yt is None:
        yt = cap
    if ig is None:
        ig = "\n\n".join(x for x in (title, cap) if x)
    return yt, ig


def clip_txt(meta: dict) -> str:
    """The .txt next to each clip: the YouTube and Instagram text, permission and the checklist."""
    yt, ig = post_fields(meta)
    lines = ["YOUTUBE TITLE:", meta.get("title") or "<write a short hook title>", "",
             "YOUTUBE DESCRIPTION:", yt, "",
             "INSTAGRAM CAPTION:", ig, "",
             meta.get("permission", ""), "",
             "BEFORE POSTING, CHECK:"]
    lines += [f"[ ] {x}" for x in checklist(bool(meta.get("hook")))]
    if meta.get("spoiler"):
        lines.append("[ ] The AI thinks this may show GTA 6 story: don't post story moments")
    return "\n".join(lines) + "\n"


# ───────────────────────────── post text (AI) ─────────────────────────────

POST_PROMPT = """You write the post text for a short vertical clip of a livestreamer. It goes on an English YouTube Shorts and Instagram Reels page that reposts streamer clips, with credit, to grow an audience.
You get the streamer, the stream's title, the hook line shown on screen, and what is said in the clip. That comes from speech-to-text, so names may be misheard: use the right spelling (for example, in a football clip "cold warmer" is "Cole Palmer").

Write:
- yt_title: a YouTube Shorts title of at most 70 characters that makes a stranger want to watch. True to the clip. No hashtags, at most one emoji.
- yt_text: one or two sentences for the YouTube description, saying what happens.
- ig_text: the start of an Instagram caption: one or two short, punchy lines; may end with one emoji. No hashtags.
- hashtags: 5 to 8 hashtags that fit this clip (the streamer, the game or sport, people or teams in it, the topic), most specific first. No generic ones like #fyp or #viral.
- spoiler: true if the clip shows or reveals GTA 6 story events, missions or cutscenes.

Never invent things that aren't in the clip. Don't add credit lines or links; those are added for you.
Reply with JSON only:
{"yt_title":"...","yt_text":"...","ig_text":"...","hashtags":["#..."],"spoiler":false}"""


def said_lines(words: list[dict], max_words: int = 14) -> list[tuple[float, str]]:
    out, cur, t0 = [], [], 0.0
    for w in words:
        if not cur:
            t0 = w["start"]
        cur.append(w["text"])
        if w["text"][-1:] in ".?!" or len(cur) >= max_words:
            out.append((t0, " ".join(cur)))
            cur = []
    if cur:
        out.append((t0, " ".join(cur)))
    return out


def clean_tags(v) -> list[str]:
    out, seen = [], set()
    for t in v if isinstance(v, list) else []:
        body = re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if 1 < len(body) <= 30 and body.lower() not in seen:
            seen.add(body.lower())
            out.append("#" + body)
    return out[:8]


def ai_post_text(cfg: dict, *, display: str, credit: str, stream_title: str, hook: str | None,
                 said: list[tuple[float, str]]) -> dict:
    """YouTube title + description text, Instagram caption text and hashtags, written by the AI."""
    if not said:
        raise ClipError("nobody talks in this clip, so the AI has nothing to go on")
    body = "\n".join(f"[{fmt_time(t)}] {x}" for t, x in said)[:6000]
    user = (f"Streamer: {display}{f' ({credit})' if credit else ''}\nStream title: {stream_title or '-'}\n"
            f"On-screen hook: {hook or '-'}\n\nWhat's said in the clip:\n{body}")
    resp = ai_request(cfg, [{"role": "system", "content": POST_PROMPT}, {"role": "user", "content": user}],
                      max_tokens=2000, timeout=90)
    content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    d = parse_json_block(content)

    def one(v, n):
        return re.sub(r"\s*#\w+", "", re.sub(r"\s+", " ", str(v or ""))).strip().strip('"')[:n]
    out = {"yt_title": one(d.get("yt_title"), 100), "yt_text": one(d.get("yt_text"), 600),
           "ig_text": re.sub(r"\s*#\w+", "", str(d.get("ig_text") or "")).strip()[:600],
           "hashtags": clean_tags(d.get("hashtags")), "spoiler": bool(d.get("spoiler"))}
    if not (out["yt_title"] or out["yt_text"] or out["ig_text"]):
        raise ClipError("the AI's reply was empty")
    return out


def fill_post(meta: dict, cfg: dict, *, display: str, credit: str, title: str, ai: dict | None) -> None:
    """Put the YouTube and Instagram text into a clip's details: the AI's, or the plain template without it."""
    link = meta.get("stream_link", "")
    tags = [("#" + re.sub(r"\W", "", display))] + ((ai or {}).get("hashtags") or list(cfg["clips"].get("hashtags") or []))
    seen, uniq = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    credit_line = f"🎮 {display} ({credit})" if credit else f"🎮 {display}"
    where = f"{credit_line}\nFull stream: {link}" if link else credit_line
    yt_tags = " ".join(uniq[:8])
    ig_tags = " ".join([t for t in uniq if t.lower() != "#shorts"][:5])  # keep Instagram to 5 hashtags
    yt_text, ig_text = (ai or {}).get("yt_text", ""), (ai or {}).get("ig_text", "")
    meta.update(
        title=title,
        yt_description="\n\n".join(x for x in (yt_text, where, yt_tags) if x),
        ig_caption="\n\n".join(x for x in (ig_text or title, where, ig_tags) if x),
        caption="\n".join(x for x in (where, yt_tags) if x),  # older readers of the clip details
        ai_text=bool(ai), spoiler=bool((ai or {}).get("spoiler")),
    )


def set_log_title(rel_file: str, title: str) -> None:
    path = ROOT / "log.csv"
    if not path.exists():
        return
    with path.open(newline="") as f:
        r = csv.DictReader(f)
        rows, cols = list(r), list(r.fieldnames or LOG_COLUMNS)
    hit = False
    for row in rows:
        if row.get("clip_file") == rel_file:
            row["title"], hit = title, True
    if hit:
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        tmp.replace(path)


def append_log(row: dict) -> None:
    path = ROOT / "log.csv"
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LOG_COLUMNS})


def clip_work(src: dict, start: float, end: float) -> Path:
    work = ROOT / "inbox" / src["key"] / f"clip-{int(start)}-{int(end)}"
    work.mkdir(parents=True, exist_ok=True)
    return work


def ensure_raw(src: dict, start: float, end: float, work: Path) -> Path:
    """The downloaded piece of the stream for this clip (downloaded again if inbox/ was cleaned)."""
    raw = work / "raw.mp4"
    if raw.exists() and raw.stat().st_size > 0:
        step("Using the piece downloaded earlier...")
        return raw
    step(f"Downloading {fmt_time(start)} → {fmt_time(end)} ({int(end - start)} s)...")
    tmp = work / "raw.part.mp4"
    grab_section(src, start, end, tmp)
    tmp.replace(raw)
    return raw


def silence_keep(cfg: dict, raw: Path, dur: float, keep_silence: bool) -> list:
    keep = [(0.0, dur)]
    if cfg["clips"].get("cut_silence", True) and not keep_silence:
        keep, removed = keep_segments(dur, silences(raw))
        if removed < 0.6:
            keep = [(0.0, dur)]
        else:
            step(f"Cutting {removed:.1f} s of silence...")
    return [list(k) for k in keep]


def render_clip(cfg: dict, work: Path, raw: Path, mi: dict, keep, words: list[dict], lay: dict,
                hook: str | None, out: Path) -> float:
    """Captions + layout → the finished 1080×1920 file. Written beside it first, then swapped in."""
    dur = mi["duration"] or sum(b - a for a, b in keep)
    total = sum(b - a for a, b in keep)
    lay_f, pos = layout_filter(lay, mi["w"], mi["h"])
    mapped, groups, _ = caption_lines(words, keep, cfg["captions"])
    (work / "captions.ass").write_text(build_ass([w for _, w in mapped], cfg["captions"], total, pos["cap_y"], hook,
                                                 pos["hook_y"], groups))
    tmp = out.with_name(out.stem + ".making.mp4")
    step("Rendering the vertical clip...")
    render(work, raw, keep, lay_f, "captions.ass", tmp, mi["fps"], dur, brand=current_brand())
    tmp.replace(out)
    return total


def load_clip_meta(path: str) -> tuple[Path, dict]:
    mp4 = Path(path).expanduser().resolve()
    js = mp4.with_suffix(".json")
    if not mp4.exists():
        raise ClipError(f"That clip isn't there: {mp4}")
    meta = json.loads(js.read_text()) if js.exists() else {}
    if not all(meta.get(k) is not None for k in ("source_url", "start", "end")):
        raise ClipError("This clip was made before clips kept their details, so its captions can't be edited. "
                        "Make it again (same times) and edit that one.")
    return mp4, meta


def clip_setup(cfg: dict, meta: dict):
    src = source_of(meta["source_url"])
    start, end = float(meta["start"]), float(meta["end"])
    work = clip_work(src, start, end)
    raw = ensure_raw(src, start, end, work)
    mi = media_info(raw)
    dur = mi["duration"] or (end - start)
    keep = meta.get("keep") or silence_keep(cfg, raw, dur, bool(meta.get("keep_silence")))
    return src, work, raw, mi, keep


def clip_words(cfg: dict, mp4: Path, meta: dict) -> list[dict]:
    """The clip's caption words; clips made before the editor existed are transcribed again (same result)."""
    if meta.get("words"):
        return meta["words"]
    _, _, raw, _, keep = clip_setup(cfg, meta)
    step("Reading the captions (on your Mac)...")
    heard, _ = transcribe(cfg, raw)
    meta.update(words=heard, heard=heard, keep=keep)
    mp4.with_suffix(".json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    return heard


def cmd_captions(a) -> None:
    """Make a clip's captions ready to edit."""
    cfg = load_config()
    mp4, meta = load_clip_meta(a.clip)
    clip_words(cfg, mp4, meta)
    say("✓ Captions ready to edit")
    report(file=meta["file"])


def cmd_recaption(a) -> None:
    """Re-make a clip with edited captions (same clip, same times, same file)."""
    cfg = load_config()
    mp4, meta = load_clip_meta(a.clip)
    if a.words:
        words = json.loads(Path(a.words).read_text())
    elif a.reset:
        words = meta.get("heard") or clip_words(cfg, mp4, meta)
    else:
        words = clip_words(cfg, mp4, meta)
    _, work, raw, mi, keep = clip_setup(cfg, meta)
    lay = load_layout(meta.get("streamer", ""))
    mode = meta.get("layout") or lay.get("mode", "full")
    lay = {**lay, "mode": mode if (mode != "camera_top" or lay.get("cam")) else "full"}
    total = render_clip(cfg, work, raw, mi, keep, words, lay, meta.get("hook") or None, mp4)
    meta.update(words=words, keep=keep, seconds=round(total, 1), captions_edited=not a.reset,
                brand=(current_brand() or {}).get("id", ""))
    mp4.with_suffix(".json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    say(f"✓ {mp4.name} re-made with your captions ({total:.0f} s)")
    report(file=meta["file"], meta=meta)


def cmd_posttext(a) -> None:
    """Write a clip's YouTube title + description and Instagram caption with the AI."""
    cfg = load_config()
    mp4, meta = load_clip_meta(a.clip)
    words = clip_words(cfg, mp4, meta)
    s_conf = load_streamers().get(str(meta.get("streamer", "")).lower()) or {}
    display = s_conf.get("display") or meta.get("display") or meta.get("streamer", "")
    credit = s_conf.get("credit", meta.get("credit", ""))
    stream = load_state()["streams"].get(meta.get("source_key", "")) or {}
    step("Writing the YouTube and Instagram text with AI...")
    ai = ai_post_text(cfg, display=display, credit=credit, stream_title=stream.get("title", ""),
                      hook=meta.get("hook") or None, said=said_lines(words))
    fill_post(meta, cfg, display=display, credit=credit, title=ai["yt_title"] or meta.get("title", ""), ai=ai)
    meta["credit"] = credit
    mp4.with_suffix(".txt").write_text(clip_txt(meta))
    mp4.with_suffix(".json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    set_log_title(meta["file"], meta["title"])
    say(f"✓ New text for {mp4.name}: {meta['title']}")
    report(file=meta["file"], meta=meta)


def cmd_makeclip(a) -> None:
    cfg = load_config()
    clean_inbox(cfg)
    rest = list(a.rest)
    if len(rest) < 2:
        raise ClipError("Usage: makeclip <link or file> [start end] <streamer>")
    link = rest.pop(0)
    times = [x for x in rest if is_time(x)]
    names = [x for x in rest if not is_time(x)]
    if len(names) != 1 or len(times) not in (0, 2):
        raise ClipError("Usage: makeclip <link or file> [start end] <streamer>  (give both start and end, or neither)")
    s_conf = get_streamer(names[0])
    name = str(s_conf["name"]).lower()
    display = s_conf.get("display", name)
    ok, perm_src = permission(s_conf)
    if not ok and not a.practice:
        raise ClipError(
            f"{display} isn't marked as allowing clips yet.\n"
            f"  Check their bio, panels, Discord or Whop, then fill in permission.source and permission.proof\n"
            f"  for {name} in {ROOT / 'streamers.yaml'}.\n"
            f"  To make a practice clip you won't post, add --practice.")

    src = source_of(link)
    if times:
        start, end = parse_time(times[0]), parse_time(times[1])
    else:
        dur0 = float(probe(src).get("duration") or 0)
        if dur0 > 180:
            raise ClipError("That video is longer than 3 minutes. Give a start and end time.")
        start, end = 0.0, dur0
    if end <= start:
        raise ClipError("The end time must be after the start time.")
    if end - start > 180:
        raise ClipError("Shorts and Reels max out at 3 minutes. Pick a shorter part (20-45 s works best).")
    if end - start > 60:
        warn(f"This clip is {int(end - start)} s long; 20-45 s usually does better.")

    st = load_state()
    clash = [x for x in st["clips"] if x.get("key") == src["key"] and x["start"] < end and start < x["end"]]
    if clash and not a.again:
        raise ClipError(f"You already made a clip from this part: {clash[0]['file']}\n  (add --again to make it anyway)")

    work = clip_work(src, start, end)
    raw = ensure_raw(src, start, end, work)
    mi = media_info(raw)
    dur = mi["duration"] or (end - start)
    keep = silence_keep(cfg, raw, dur, a.keep_silence)

    step("Writing captions (on your Mac)...")
    heard, _ = transcribe(cfg, raw)
    words, n_fixed = apply_rules(heard, rules_for(name))
    if n_fixed:
        say(f"    fixed {n_fixed} misheard word(s) from your word fixes (fixes.yaml)")

    lay = load_layout(name)
    if a.layout:
        lay = {**lay, "mode": a.layout}
        if a.layout == "camera_top" and not lay.get("cam"):
            raise ClipError(f"No camera box saved for {name}. Run: clip camera {name} <stream link> <time>")
    hook = a.hook.strip() if a.hook else None

    day = dt.date.today().isoformat()
    out_dir = ROOT / "out" / ("practice" if a.practice else day)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = slug(src["id"], 28) if src["kind"] in ("twitch-clip", "kick-clip") else fmt_dash(start)
    out = out_dir / f"{'PRACTICE-' if a.practice else ''}{name}-{tag}.mp4"
    total = render_clip(cfg, work, raw, mi, keep, words, lay, hook, out)
    forget_unbranded(out)

    moment = None
    for m in (st["streams"].get(src["key"]) or {}).get("moments", []):
        overlap = min(m["end"], end) - max(m["start"], start)
        if overlap > 0.5 * (end - start):
            moment = m
            break
    ai = None
    if cfg["clips"].get("ai_text", True):
        step("Writing the YouTube and Instagram text with AI...")
        try:
            ai = ai_post_text(cfg, display=display, credit=s_conf.get("credit", ""), hook=hook, said=said_lines(words),
                              stream_title=(st["streams"].get(src["key"]) or {}).get("title", ""))
        except AISkip as e:
            say(f"    skipped: {e}. Using the plain title and hashtags.")
        except (ClipError, json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            warn(f"The AI text didn't work ({e}); using the plain title and hashtags.")
    title = a.title or (ai or {}).get("yt_title") or (moment or {}).get("title") or ""
    p = s_conf.get("permission") or {}
    meta = {
        "version": 2, "file": str(out.relative_to(ROOT)), "streamer": name, "display": display,
        "credit": s_conf.get("credit", ""), "source_url": src["url"], "source_key": src["key"],
        "source_kind": src["kind"], "stream_link": link_at(src, start), "start": start, "end": end,
        "seconds": round(total, 1), "practice": bool(a.practice), "hook": hook or "",
        "permission": f"PERMISSION: {perm_src} · proof: {p.get('proof', '') or '-'} · platforms: "
                      f"{', '.join(p.get('platforms') or []) or '-'} · monetize: {'yes' if p.get('monetize') else 'no'}",
        "made_on": day, "layout": lay.get("mode", "full"), "keep_silence": bool(a.keep_silence),
        "keep": keep, "words": words, "heard": heard, "brand": (current_brand() or {}).get("id", ""),
    }
    fill_post(meta, cfg, display=display, credit=s_conf.get("credit", ""), title=title, ai=ai)
    if meta["spoiler"]:
        warn("The AI thinks this clip may show GTA 6 story. Don't post story moments.")
    out.with_suffix(".txt").write_text(clip_txt(meta))
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))

    if not a.practice:
        append_log({"made_on": day, "clip_file": str(out.relative_to(ROOT)), "streamer": name,
                    "source_link": link_at(src, start), "start": fmt_time(start), "end": fmt_time(end),
                    "title": title or ""})
        st["clips"].append({"key": src["key"], "streamer": name, "start": start, "end": end,
                            "file": str(out), "made_on": day})
        save_state(st)

    say("")
    say(f"✓ {out}  ({total:.0f} s, 1080×1920)")
    say(f"  Title, credit, hashtags and checklist: {out.with_suffix('.txt')}")
    if lay.get("mode") != "camera_top" and not load_layout(name).get("cam"):
        say(f"  Layout: full frame on a blurred background. For camera-on-top, run: clip camera {name} <link> <time>")
    report(file=meta["file"], meta=meta)


# ───────────────────────────── camera box ─────────────────────────────

def grab_frame(src: dict, t: float, out: Path) -> None:
    info = probe(src, "bv*[height<=1080]/b")
    inp = info["_input"][0]
    run([FFMPEG, "-v", "error", "-y", *ff_input(inp, t), "-frames:v", "1", str(out)])


def select_box(frame: Path, title: str):
    import tkinter as tk
    mi = media_info(frame)
    W, H = mi["w"], mi["h"]
    scale = max(1.0, W / 1280)
    disp = frame.with_name(frame.stem + "-display.png")
    run([FFMPEG, "-v", "error", "-y", "-i", frame, "-vf", f"scale={int(W / scale)}:-2", disp])
    root = tk.Tk()
    root.title(title)
    img = tk.PhotoImage(file=str(disp))
    cv = tk.Canvas(root, width=img.width(), height=img.height(), highlightthickness=0, cursor="crosshair")
    cv.pack()
    cv.create_image(0, 0, image=img, anchor="nw")
    tk.Label(root, text="Drag a box around the streamer's camera, then press Enter.  (Esc to cancel)",
             font=("Helvetica", 15)).pack(fill="x", pady=6)
    state = {"rect": None, "x0": 0, "y0": 0, "box": None, "done": None}

    def down(e):
        state["x0"], state["y0"] = e.x, e.y
        if state["rect"]:
            cv.delete(state["rect"])
        state["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#00FF66", width=3)

    def drag(e):
        cv.coords(state["rect"], state["x0"], state["y0"], e.x, e.y)

    def up(e):
        x0, y0, x1, y1 = cv.coords(state["rect"])
        state["box"] = (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

    def done(e=None):
        b = state["box"]
        if b and b[2] > 10 and b[3] > 10:
            state["done"] = b
            root.destroy()

    cv.bind("<ButtonPress-1>", down)
    cv.bind("<B1-Motion>", drag)
    cv.bind("<ButtonRelease-1>", up)
    root.bind("<Return>", done)
    root.bind("<Escape>", lambda e: root.destroy())
    root.lift()
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()
    if not state["done"]:
        return None
    x, y, w, h = (v * scale for v in state["done"])
    return x, y, w, h, W, H


def render_preview(frame: Path, lay: dict, out: Path, cfg: dict) -> None:
    mi = media_info(frame)
    lay_f, pos = layout_filter(lay, mi["w"], mi["h"], src_label="[0:v]")
    words = [{"text": "CAPTIONS", "start": 0.0, "end": 3.0}, {"text": "GO", "start": 3.0, "end": 6.0},
             {"text": "HERE", "start": 6.0, "end": 9.0}]
    ass_path = frame.with_name("preview.ass")
    ass_path.write_text(build_ass(words, cfg["captions"], 10, pos["cap_y"], "Hook line goes here", pos["hook_y"]))
    b = current_brand()
    base, logo_in, logo_f = "[lay]", [], ""
    if b:
        base, logo_in, logo_f = "[layb]", ["-i", b["path"]], ";" + brand_filter(b, "[lay]", "[layb]", 1)
    run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-t", "1", "-i", frame.name, *logo_in, "-filter_complex",
         f"{lay_f}{logo_f};{base}ass=filename={ass_path.name}:fontsdir={FONTS_DIR}[v]", "-map", "[v]",
         "-ss", "0.1", "-frames:v", "1", str(out)], cwd=frame.parent)
    ass_path.unlink(missing_ok=True)


def cmd_brand(a) -> None:
    """clip brand logo <picture> | preview | apply"""
    cfg = load_config()
    if a.action == "logo":
        if not a.file:
            raise ClipError("Usage: clip brand logo <picture of your logo>")
        step("Cutting the logo out of its background...")
        out = prepare_logo(Path(a.file).expanduser())
        say(f"✓ Logo ready: {out}")
        a.action = "preview"
    if a.action == "preview":
        frames = sorted((ROOT / "layouts").glob("*-frame.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not frames:  # no stream frame yet: a plain one
            frames = [BRAND_DIR / "sample-frame.png"]
            BRAND_DIR.mkdir(parents=True, exist_ok=True)
            run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "gradients=s=1920x1080:c0=0x24324a:c1=0x4a2a5a",
                 "-frames:v", "1", frames[0]])
        frame = frames[0]
        name = frame.stem.removesuffix("-frame")
        lay = load_layout(name)
        shots = []
        for mode in (["camera_top"] if lay.get("cam") else []) + ["full"]:
            o = BRAND_DIR / f"preview-{mode}.png"
            render_preview(frame, {**lay, "mode": mode}, o, cfg)
            shots.append(o)
        out = BRAND_DIR / "preview.png"
        stack = "".join(f"[{i}:v]" for i in range(len(shots))) + f"hstack=inputs={len(shots)}," if len(shots) > 1 else ""
        run([FFMPEG, "-v", "error", "-y", *sum((["-i", str(s)] for s in shots), []), "-filter_complex",
             f"{stack or '[0:v]'}scale=-2:960[v]", "-map", "[v]", "-frames:v", "1", out])
        say(f"✓ Preview: {out}")
        report(preview=str(out))
        return
    if a.action == "apply":  # make every finished clip match the logo settings (logo on or off)
        b = current_brand()
        want = b["id"] if b else ""
        clips = sorted(p for p in (ROOT / "out").rglob("*.mp4")
                       if p.with_suffix(".txt").exists() and ".making" not in p.name)
        todo = [p for p in clips if clip_brand(p) != want]
        if not todo:
            say(f"✓ All {len(clips)} clips already match your logo settings")
            report(done=0, failed=0, total=len(clips))
            return
        step(f"{'Adding the logo to' if b else 'Taking the logo off'} {len(todo)} clip(s)...")
        done, failed = 0, []
        for i, mp4 in enumerate(todo, 1):
            say(f"[{i}/{len(todo)}] {mp4.name}")
            try:
                meta = json.loads(mp4.with_suffix(".json").read_text()) if mp4.with_suffix(".json").exists() else None
                remade = False
                if meta and all(meta.get(k) is not None for k in ("source_url", "start", "end")):
                    try:  # best: make it again from its stream piece, so it's encoded only once
                        words = clip_words(cfg, mp4, meta)
                        _, work, raw, mi, keep = clip_setup(cfg, meta)
                        lay = load_layout(meta.get("streamer", ""))
                        mode = meta.get("layout") or lay.get("mode", "full")
                        lay = {**lay, "mode": mode if (mode != "camera_top" or lay.get("cam")) else "full"}
                        render_clip(cfg, work, raw, mi, keep, words, lay, meta.get("hook") or None, mp4)
                        remade = True
                    except ClipError as e:
                        warn(f"couldn't make it again from the stream ({e}); adding the logo on top instead")
                if not remade:
                    brand_finished(mp4, b)
                if meta is not None:
                    meta["brand"] = want
                    mp4.with_suffix(".json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
                else:
                    idx = UNBRANDED / "index.json"
                    d = json.loads(idx.read_text()) if idx.exists() else {}
                    d[str(mp4.relative_to(ROOT))] = want
                    idx.write_text(json.dumps(d, indent=1))
                done += 1
            except (ClipError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as e:
                failed.append(mp4.name)
                warn(f"{mp4.name}: {e}")
        say(f"✓ {done} clip(s) updated" + (f"; {len(failed)} didn't work: {', '.join(failed)}" if failed else ""))
        report(done=done, failed=len(failed), total=len(clips))
        return
    raise ClipError("Usage: clip brand logo <picture> | clip brand preview | clip brand apply")


def cmd_camera(a) -> None:
    cfg = load_config()
    s_conf = get_streamer(a.streamer)
    name = str(s_conf["name"]).lower()
    lay_path = ROOT / "layouts" / f"{name}.json"
    lay_path.parent.mkdir(parents=True, exist_ok=True)
    if a.none:
        lay_path.write_text(json.dumps({"mode": "full"}, indent=2))
        say(f"✓ {name}: full-frame layout (no camera on top)")
        report(layout=str(lay_path), preview="")
        return
    frame = ROOT / "layouts" / f"{name}-frame.png"
    if a.frame:  # a frame the page already grabbed
        if not a.box:
            raise ClipError("--frame needs --box X,Y,W,H")
        shutil.copyfile(a.frame, frame)
    else:
        if not a.link or a.time is None:
            raise ClipError("Usage: clip camera <streamer> <stream link or file> <time>")
        src = source_of(a.link)
        step("Grabbing a frame...")
        grab_frame(src, parse_time(a.time), frame)
    mi = media_info(frame)
    if a.box:
        x, y, w, h = (float(v) for v in a.box.split(","))
        W, H = mi["w"], mi["h"]
    else:
        picked = select_box(frame, f"{s_conf.get('display', name)}: camera position")
        if not picked:
            say("Cancelled; nothing saved.")
            return
        x, y, w, h, W, H = picked
    x, y = max(0.0, x), max(0.0, y)
    w, h = min(w, W - x), min(h, H - y)
    if w < 10 or h < 10:
        raise ClipError("That camera box is too small.")
    lay = {"mode": "camera_top", "cam": [x / W, y / H, w / W, h / H], "game_center": 0.5,
           "set_from": a.set_from or f"{a.link} at {a.time}", "set_on": dt.date.today().isoformat()}
    lay_path.write_text(json.dumps(lay, indent=2))
    preview = ROOT / "layouts" / f"{name}-preview.png"
    render_preview(frame, lay, preview, cfg)
    say(f"✓ Saved {lay_path}")
    say(f"  Preview: {preview}")
    report(layout=str(lay_path), preview=str(preview))
    if not a.box:
        subprocess.run(["open", str(preview)])


def cmd_frame(a) -> None:
    """Grab one frame of a stream (used by the Clipping page's camera-box picker)."""
    src = source_of(a.link)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    step(f"Grabbing the frame at {fmt_time(parse_time(a.time))}...")
    grab_frame(src, parse_time(a.time), out)
    mi = media_info(out)
    say(f"✓ {out}")
    report(frame=str(out), w=mi["w"], h=mi["h"])


# ───────────────────────────── record + check ─────────────────────────────

def cmd_record(a) -> None:
    s_conf = get_streamer(a.streamer)
    ch = s_conf.get("channel")
    if not ch:
        raise ClipError(f"No channel link for {a.streamer} in streamers.yaml.")
    live_dir = ROOT / "inbox" / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    out = live_dir / f"{s_conf['name']}-{dt.datetime.now():%Y%m%d-%H%M}.ts"
    say(f"Recording {ch} → {out}\nPress Ctrl+C to stop. Afterwards: findmoments \"{out}\" --streamer {s_conf['name']}")
    report(file=str(out), streamer=str(s_conf["name"]).lower())
    subprocess.run([*STREAMLINK, "-o", str(out), ch, "best"])


def cmd_check(a) -> None:
    cfg = load_config()

    def line(ok, label, detail=""):
        say(f"  {'✓' if ok else '✗'} {label}{(': ' + detail) if detail else ''}")

    say("Tools")
    filters = run([FFMPEG, "-hide_banner", "-filters"], check=False)
    line(" ass " in filters, "ffmpeg with captions", FFMPEG)
    ytv = run([*YTDLP, "--version"], check=False).strip()
    slv = run([*STREAMLINK, "--version"], check=False).strip()
    line(bool(ytv), "yt-dlp", ytv or "not working")
    line(bool(slv), "streamlink", slv or "not working")
    mdir = model_path(cfg)
    say(f"  · project folder: {ROOT}")
    line((mdir / "model.safetensors").exists(), "caption model (Parakeet v3)", str(mdir))
    line(Path(FONTS_DIR, f"{cfg['captions']['font']}.ttf").exists(), "caption font", cfg["captions"]["font"])
    r = cfg["rank"]
    if str(r.get("provider", "none")).lower() == "none":
        line(True, "AI ranking", "off (findmoments ranks by chat + volume)")
    else:
        s = ai_status(cfg)
        detail = f"{r['model']} via {r['base_url']} · key {'from ' + s['key'] if s['key'] else 'NOT found'}"
        line(s["on"], "AI (ranking + post text)", detail + (f" · {s['paused']}" if s["paused"] else ""))
    say("\nStreamers")
    for name, s in load_streamers().items():
        ok, src = permission(s)
        lay = load_layout(name)
        cam = "camera on top" if lay.get("mode") == "camera_top" else "full frame (no camera box yet)"
        line(ok, f"{s.get('display', name)}", f"clips {'allowed (' + src + ')' if ok else 'NOT confirmed'} · {cam}")
    du = shutil.disk_usage(Path.home())
    say(f"\nDisk: {du.free / 1e9:.0f} GB free")


# ───────────────────────────── entry points ─────────────────────────────

def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="clip", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("findmoments", help="find the best moments in a stream")
    f.add_argument("link")
    f.add_argument("--streamer")
    f.add_argument("--from", dest="from_")
    f.add_argument("--to")
    f.add_argument("--top", type=int)
    f.add_argument("--candidates", type=int)
    f.add_argument("--full-audio", action="store_true", help="download the whole audio and use volume spikes everywhere")
    f.add_argument("--again", action="store_true")

    m = sub.add_parser("makeclip", help="make a captioned vertical clip")
    m.add_argument("rest", nargs="+", metavar="ARGS", help="<link or file> [start end] <streamer>")
    m.add_argument("--hook")
    m.add_argument("--title")
    m.add_argument("--layout", choices=["full", "camera_top"])
    m.add_argument("--keep-silence", action="store_true")
    m.add_argument("--practice", action="store_true", help="practice clip: no permission needed, not logged")
    m.add_argument("--again", action="store_true")

    c = sub.add_parser("camera", help="set where a streamer's camera sits")
    c.add_argument("streamer")
    c.add_argument("link", nargs="?")
    c.add_argument("time", nargs="?")
    c.add_argument("--box", help="X,Y,W,H in pixels of the grabbed frame")
    c.add_argument("--none", action="store_true", help="no camera: use the full-frame layout")
    c.add_argument("--frame", help="use this already-grabbed frame (needs --box)")
    c.add_argument("--set-from", help="note saved with the layout: where the frame came from")

    cp = sub.add_parser("captions", help="make a clip's captions ready to edit")
    cp.add_argument("clip")
    rc = sub.add_parser("recaption", help="re-make a clip with edited captions")
    rc.add_argument("clip")
    rc.add_argument("--words", help="JSON file with the edited caption words")
    rc.add_argument("--reset", action="store_true", help="go back to the captions as they were heard")
    pt = sub.add_parser("posttext", help="write a clip's YouTube and Instagram text with the AI")
    pt.add_argument("clip")
    br = sub.add_parser("brand", help="your logo on every clip: brand logo <picture> | brand preview | brand apply")
    br.add_argument("action", choices=["logo", "preview", "apply"])
    br.add_argument("file", nargs="?")

    fr = sub.add_parser("frame", help="grab one frame of a stream to a file")
    fr.add_argument("link")
    fr.add_argument("time")
    fr.add_argument("out")

    r = sub.add_parser("record", help="record a live stream")
    r.add_argument("streamer")

    sub.add_parser("check", help="check tools, model, key and streamers")

    a = ap.parse_args(argv)
    try:
        {"findmoments": cmd_findmoments, "makeclip": cmd_makeclip, "camera": cmd_camera, "frame": cmd_frame,
         "captions": cmd_captions, "recaption": cmd_recaption, "posttext": cmd_posttext, "brand": cmd_brand,
         "record": cmd_record, "check": cmd_check}[a.cmd](a)
    except ClipError as e:
        say(f"✗ {e}")
        return 1
    except KeyboardInterrupt:
        say("\nStopped.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
