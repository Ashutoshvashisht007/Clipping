"""Builds the Clipping demo video, start to finish, with no editing by hand.

    demo/make_demo all       voice → cards → record the page → build the video (about 10 minutes)
    demo/make_demo voices    short samples of a few voices, to pick one for script.yaml
    demo/make_demo voice     (re)make the narration from script.yaml
    demo/make_demo record    record the page again (a throwaway copy of the folder, so your clips are untouched)
    demo/make_demo build     put the video together from what's there (after changing the words or adding music)

The video lands in demo/out/clipping-demo.mp4.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import yaml

DEMO = Path(__file__).resolve().parent
ROOT = DEMO.parent
WORK, OUT = DEMO / "work", DEMO / "out"
FFBIN = Path("/opt/homebrew/opt/ffmpeg-full/bin")
FFMPEG, FFPROBE = str(FFBIN / "ffmpeg"), str(FFBIN / "ffprobe")
VOICE_PY = DEMO / ".venv-voice" / "bin" / "python"
W, H, FPS = 1920, 1080, 30
SCREEN = (144, 36, 1632, 918)      # where the recorded page sits on the 1920×1080 frame
HOOK_CLIP = (679, 40, 562, 1000)   # the finished clip, opening shot
END_CLIP = (1250, 60, 506, 900)    # the finished clip, closing card
LEAD, GAP, TAIL = 0.45, 0.32, 0.7  # narration: silence before, between and after lines
SQUEEZE = 3.5                       # a long wait is shown sped up to about this many seconds
SAMPLE_VOICES = ["af_heart", "af_bella", "am_michael", "am_fenrir", "bf_emma", "bm_george"]


def step(msg: str) -> None:
    print(f"• {msg}", flush=True)


def run(cmd: list, **kw) -> None:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)
    if r.returncode:
        sys.exit(f"✗ {Path(str(cmd[0])).name} failed:\n{(r.stderr or r.stdout)[-3000:]}")


def load_script() -> dict:
    return yaml.safe_load((DEMO / "script.yaml").read_text())


def lines_of(scene: dict) -> list[dict]:
    out = []
    for x in scene.get("say") or []:
        if isinstance(x, dict):
            out.append({"say": x["say"], "show": x.get("show", x["say"]), "hl": x.get("hl", "")})
        else:
            out.append({"say": x, "show": x, "hl": ""})
    return out


def wav_seconds(p: Path) -> float:
    with wave.open(str(p)) as w:
        return w.getnframes() / w.getframerate()


def need(path: Path, what: str) -> Path:
    if not path.exists():
        sys.exit(f"✗ {what} isn't there yet ({path}). Run: demo/make_demo all")
    return path


# ───────────────────────────── voice ─────────────────────────────

def tts(items: list[dict], voice: str, speed: float) -> None:
    if not VOICE_PY.exists():
        sys.exit("✗ The voice tools aren't installed (demo/.venv-voice). See demo/README.md.")
    job = WORK / "tts-job.json"
    job.parent.mkdir(parents=True, exist_ok=True)
    job.write_text(json.dumps({"voice": voice, "speed": speed, "items": items}))
    r = subprocess.run([str(VOICE_PY), str(DEMO / "tts.py"), str(job)], text=True)
    if r.returncode:
        sys.exit("✗ Making the voice didn't work (see above).")


def cmd_voices() -> None:
    text = ("Hi! This is how the Clipping demo sounds in this voice. "
            "It turns a two-hour stream into a ready-to-post vertical clip, in a few minutes.")
    d = OUT / "voices"
    for v in SAMPLE_VOICES:
        step(f"Voice {v}...")
        tts([{"text": text, "out": str(d / f"{v}.wav")}], v, 1.0)
    print(f"\n✓ Samples in {d}. Put the one you like in script.yaml (voice: ...), then: demo/make_demo voice build")


def cmd_voice() -> None:
    sc = load_script()
    voice, speed = sc.get("voice", "af_heart"), float(sc.get("speed", 1.0))
    items, plan = [], {}
    for s in sc["scenes"]:
        for i, ln in enumerate(lines_of(s)):
            items.append({"text": ln["say"], "out": str(WORK / "voice" / f"{s['id']}-{i}.wav")})
    step(f"Narration with Kokoro, voice {voice}...")
    tts(items, voice, speed)
    for s in sc["scenes"]:
        t, lines = LEAD, []
        for i, ln in enumerate(lines_of(s)):
            p = WORK / "voice" / f"{s['id']}-{i}.wav"
            d = wav_seconds(p)
            lines.append({"wav": str(p), "start": t, "end": t + d, "show": ln["show"], "hl": ln["hl"]})
            t += d + GAP
        plan[s["id"]] = {"lines": lines, "seconds": (t - GAP + TAIL) if lines else float(s.get("seconds", 5))}
    (WORK / "voice" / "plan.json").write_text(json.dumps(plan, indent=1))
    total = sum(p["seconds"] for p in plan.values())
    print(f"✓ Narration: {sum(len(p['lines']) for p in plan.values())} lines, {total:.0f} s spoken (plus the waits on screen)")


def narration_wav(scene_id: str, plan: dict, seconds: float) -> Path:
    """One WAV for the scene: its lines placed at their start times, silence elsewhere."""
    import numpy as np
    rate = 24000
    buf = np.zeros(int(seconds * rate) + rate, dtype=np.int16)
    for ln in plan[scene_id]["lines"]:
        with wave.open(ln["wav"]) as w:
            rate_ = w.getframerate()
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        assert rate_ == rate
        a = int(ln["start"] * rate)
        buf[a: a + len(x)] = x[: max(0, len(buf) - a)]
    out = WORK / "build" / f"{scene_id}-voice.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(buf[: int(seconds * rate)].tobytes())
    return out


# ───────────────────────────── cards ─────────────────────────────

def cmd_cards() -> None:
    sc = load_script()
    data = {k: sc.get(k, "") for k in ("title_line", "credit_line", "page_line", "stack_line", "music_credit")}
    hook = next((s for s in sc["scenes"] if s["id"] == "hook"), {})
    data["hook_note"] = hook.get("note", "")
    data["layout"] = {"screen": SCREEN, "hook_clip": HOOK_CLIP, "end_clip": END_CLIP}
    data["highlights"] = {s["id"]: sorted({ln["hl"] for ln in lines_of(s) if ln["hl"]})
                          for s in sc["scenes"] if s["kind"] == "card" and any(ln["hl"] for ln in lines_of(s))}
    data["masks"] = [[SCREEN[2], SCREEN[3], 16], [HOOK_CLIP[2], HOOK_CLIP[3], 22], [END_CLIP[2], END_CLIP[3], 22]]
    f = WORK / "cards-data.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data))
    step("Drawing the title cards...")
    run(["node", DEMO / "cards.js", f, WORK / "cards"], cwd=DEMO)


# ───────────────────────────── record ─────────────────────────────

def free_port() -> int:
    for p in range(8790, 8800):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)):
                return p
    sys.exit("✗ No free port between 8790 and 8799.")


def make_root(sc: dict) -> Path:
    """A throwaway copy of the Clipping folder: same tools and settings, none of your streams or clips."""
    root = WORK / "root"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for name in (".venv", "models", "ui", "bin", ".env"):
        if (ROOT / name).exists():
            (root / name).symlink_to(ROOT / name)
    shutil.copy(ROOT / "config.yaml", root / "config.yaml")
    if (ROOT / "fixes.yaml").exists():
        shutil.copy(ROOT / "fixes.yaml", root / "fixes.yaml")
    shown = [n.lower() for n in sc.get("streamers_shown") or [sc["streamer"]]]
    data = yaml.safe_load((ROOT / "streamers.yaml").read_text()) or {}
    keep = [s for s in data.get("streamers", []) if str(s.get("name", "")).lower() in shown]
    keep.sort(key=lambda s: shown.index(str(s["name"]).lower()))
    (root / "streamers.yaml").write_text(yaml.safe_dump({"streamers": keep}, sort_keys=False, allow_unicode=True))
    shutil.copytree(ROOT / "brand", root / "brand", ignore=shutil.ignore_patterns("previews"))
    (root / "layouts").mkdir()
    for n in shown:
        for f in (ROOT / "layouts").glob(f"{n}*"):
            if f.name in (f"{n}.json", f"{n}-preview.png"):
                shutil.copy(f, root / "layouts" / f.name)
    # reuse audio already downloaded for this stream (it's only a cache), so the recording is quicker
    sys.path.insert(0, str(ROOT / "bin"))
    os.environ["CLIPKIT_ROOT"] = str(root)
    import clipkit
    key = clipkit.source_of(sc["stream"])["key"]
    for audio in (ROOT / "inbox" / key).glob("audio.*"):
        if ".part" not in audio.name:
            (root / "inbox" / key).mkdir(parents=True, exist_ok=True)
            (root / "inbox" / key / audio.name).symlink_to(audio)
    return root


def cmd_record() -> None:
    sc = load_script()
    plan_f = WORK / "voice" / "plan.json"
    if not plan_f.exists():
        cmd_voice()
    vplan = json.loads(plan_f.read_text())
    root = make_root(sc)
    port = free_port()
    step(f"Starting a throwaway Clipping page on port {port}...")
    env = {**os.environ, "CLIPKIT_ROOT": str(root)}
    log = open(WORK / "server.log", "w")
    srv = subprocess.Popen([str(ROOT / ".venv" / "bin" / "python"), "-I", str(ROOT / "bin" / "clipui.py"),
                            "--port", str(port), "--no-open"], cwd=root, env=env, stdout=log, stderr=log)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            sys.exit("✗ The throwaway page didn't start (see demo/work/server.log).")
        targets = {s["id"]: vplan[s["id"]]["seconds"] for s in sc["scenes"] if s["kind"] == "screen"}
        rec_plan = WORK / "record-plan.json"
        rec_plan.write_text(json.dumps({"stream": sc["stream"], "streamer": sc["streamer"], "squeeze": SQUEEZE,
                                        "targets": targets}))
        step("Recording the page (finding moments and making the clip for real: about 5–8 minutes)...")
        r = subprocess.run(["node", str(DEMO / "record.js"), str(port), str(WORK / "rec"), str(rec_plan)], cwd=DEMO)
        if r.returncode:
            sys.exit("✗ The recording stopped (see above; a picture of the page is in demo/work/rec/failed.png).")
    finally:
        srv.send_signal(signal.SIGINT)
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
        log.close()
    tl = json.loads((WORK / "rec" / "timeline.json").read_text())
    clip = root / tl["clip"]
    shutil.copy(clip, WORK / "clip.mp4")
    step("Turning the frames into a video...")
    frames = json.loads((WORK / "rec" / "frames.json").read_text())
    lst = WORK / "rec" / "frames.txt"
    with open(lst, "w") as f:
        f.write("ffconcat version 1.0\n")
        f.write(f"file 'frames/{frames[0][0]}'\nduration {frames[0][1]:.4f}\n")  # hold the first frame from 0
        for (name, t), (_, t2) in zip(frames, frames[1:] + [[None, tl["end"]]]):
            f.write(f"file 'frames/{name}'\nduration {max(0.001, t2 - t):.4f}\n")
        f.write(f"file 'frames/{frames[-1][0]}'\n")
    run([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-fps_mode", "cfr", "-r", FPS,
         "-vf", f"scale={W}:{H}:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
         WORK / "screen.mp4"], cwd=WORK / "rec")
    shutil.rmtree(WORK / "rec" / "frames", ignore_errors=True)
    print(f"✓ Recorded {tl['end']:.0f} s of the page ({len(frames)} frames)")


# ───────────────────────────── build ─────────────────────────────

def ass_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def ass_file(name: str, events: list[tuple], left: bool = False) -> Path:
    """Captions for one scene: (start, end, text, style)."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Helvetica Neue,40,&H00F3F3F6,&H00F3F3F6,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,0,2,2,{'150' if left else '230'},{'720' if left else '230'},30,1
Style: Badge,Helvetica Neue,30,&H00101010,&H00101010,&H0000E6FF,&H0000E6FF,1,0,0,0,100,100,0,0,3,9,0,9,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if left:
        head = head.replace(",2,150,720,", ",1,150,720,")
    rows = []
    for a, b, text, style in events:
        # the speed label sits in the frame's bottom-right corner, where the page is usually empty
        pos = r"{\an3\pos(%d,%d)}" % (SCREEN[0] + SCREEN[2] - 26, SCREEN[1] + SCREEN[3] - 22) if style == "Badge" else ""
        rows.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},{style},,0,0,0,,{pos}{text.replace(chr(10), ' ')}")
    p = WORK / "build" / f"{name}.ass"
    p.write_text(head + "\n".join(rows) + "\n")
    return p


def sub_events(scene_id: str, plan: dict) -> list[tuple]:
    return [(ln["start"], ln["end"] + 0.25, ln["show"], "Sub") for ln in plan[scene_id]["lines"]]


def encode(cmd_inputs: list, graph: str, seconds: float, out: Path, audio_label: str) -> None:
    run([FFMPEG, "-v", "error", "-y", *cmd_inputs, "-filter_complex", graph, "-map", "[v]", "-map", audio_label,
         "-t", f"{seconds:.3f}", "-r", FPS, "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", out])


def ffpath(p: Path) -> str:  # a path inside an ffmpeg filter (the ass filter wants : and \ escaped)
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_screen(scene: dict, tl_scene: dict, plan: dict, out: Path) -> float:
    t0, t1 = tl_scene["t0"], tl_scene["t1"]
    pieces, t, badges, shown = [], t0, [], 0.0
    for a, b in tl_scene["waits"]:
        if a > t:
            pieces.append((t, a, 1.0))
        speed = (b - a) / SQUEEZE if b - a > SQUEEZE * 1.4 else 1.0
        pieces.append((a, b, speed))
        t = b
    if t1 > t:
        pieces.append((t, t1, 1.0))
    for a, b, sp in pieces:
        d = (b - a) / sp
        if sp > 1:
            badges.append((shown + 0.15, shown + d - 0.1, f"{round(sp)}× speed", "Badge"))
        shown += d
    seconds = max(shown, plan[scene["id"]]["seconds"])
    ass = ass_file(scene["id"], sub_events(scene["id"], plan) + badges)
    voice = narration_wav(scene["id"], plan, seconds)
    n = len(pieces)
    g = [f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n)) if n > 1 else "[0:v]null[s0]"]
    for i, (a, b, sp) in enumerate(pieces):
        g.append(f"[s{i}]trim=start={a - t0:.3f}:end={b - t0:.3f},setpts=(PTS-STARTPTS)/{sp:.4f}[p{i}]")
    g.append("".join(f"[p{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0,fps={FPS},"
             f"scale={SCREEN[2]}:{SCREEN[3]}:flags=lanczos,tpad=stop_mode=clone:stop_duration={seconds - shown + 0.2:.3f},"
             f"format=yuva420p[sc]")
    g.append("[2:v]format=gray[m]; [sc][m]alphamerge[scm]")
    g.append(f"[1:v][scm]overlay={SCREEN[0]}:{SCREEN[1]}:shortest=0,ass='{ffpath(ass)}',format=yuv420p[v]")
    g.append("[3:a]aresample=48000,apad[a]")
    cards = WORK / "cards"
    encode(["-ss", f"{t0:.3f}", "-t", f"{t1 - t0 + 0.3:.3f}", "-i", WORK / "screen.mp4",
            "-loop", "1", "-framerate", FPS, "-i", cards / "bg.png",
            "-loop", "1", "-framerate", FPS, "-i", cards / f"mask-{SCREEN[2]}x{SCREEN[3]}x16.png", "-i", voice],
           "; ".join(g), seconds, out, "[a]")
    return seconds


def build_card(scene: dict, plan: dict, out: Path) -> float:
    seconds = plan[scene["id"]]["seconds"] + 0.3
    ass = ass_file(scene["id"], sub_events(scene["id"], plan))
    voice = narration_wav(scene["id"], plan, seconds)
    g = (f"[0:v]fps={FPS},fade=t=in:st=0:d=0.35,fade=t=out:st={seconds - 0.35:.3f}:d=0.35,ass='{ffpath(ass)}',format=yuv420p[v]; "
         "[1:a]aresample=48000,apad[a]")
    lines = plan[scene["id"]]["lines"]
    cards = WORK / "cards"
    if any(ln.get("hl") for ln in lines):  # the card lights up the part each line talks about
        lst = WORK / "build" / f"{scene['id']}-cards.txt"
        with open(lst, "w") as f:
            f.write("ffconcat version 1.0\n")
            for i, ln in enumerate(lines):
                a = 0.0 if i == 0 else ln["start"] - 0.15
                b = seconds if i == len(lines) - 1 else lines[i + 1]["start"] - 0.15
                pic = cards / (f"{scene['id']}-hl-{ln['hl']}.png" if ln.get("hl") else f"{scene['id']}.png")
                f.write(f"file '{pic}'\nduration {b - a:.3f}\n")
            f.write(f"file '{pic}'\n")
        src = ["-f", "concat", "-safe", "0", "-i", lst]
    else:
        src = ["-loop", "1", "-framerate", FPS, "-i", cards / f"{scene['id']}.png"]
    encode([*src, "-i", voice], g, seconds, out, "[a]")
    return seconds


def build_clip(scene: dict, plan: dict, out: Path, opening: bool) -> float:
    clip = need(WORK / "clip.mp4", "The finished clip")
    x, y, w, h = HOOK_CLIP if opening else END_CLIP
    seconds = float(scene.get("seconds", 6)) if opening else plan[scene["id"]]["seconds"] + 1.2
    card = "hook.png" if opening else "end.png"
    ass = ass_file(scene["id"], [] if opening else sub_events(scene["id"], plan), left=not opening)
    fade = (f"fade=t=in:st=0:d=0.5" if opening else f"fade=t=out:st={seconds - 1.0:.3f}:d=1.0")
    g = [f"[1:v]scale={w}:{h}:flags=lanczos,tpad=stop_mode=clone:stop_duration={seconds:.2f},format=yuva420p[c]",
         "[2:v]format=gray[m]; [c][m]alphamerge[cm]",
         f"[0:v][cm]overlay={x}:{y}:shortest=0,ass='{ffpath(ass)}',{fade},format=yuv420p[v]"]
    inputs = ["-loop", "1", "-framerate", FPS, "-i", WORK / "cards" / card, "-i", clip,
              "-loop", "1", "-framerate", FPS, "-i", WORK / "cards" / f"mask-{w}x{h}x22.png"]
    if opening:  # the clip's own sound
        g.append(f"[1:a]aresample=48000,afade=t=out:st={seconds - 0.6:.2f}:d=0.6,apad[a]")
    else:
        inputs += ["-i", narration_wav(scene["id"], plan, seconds)]
        g.append("[3:a]aresample=48000,apad[a]")
    encode(inputs, "; ".join(g), seconds, out, "[a]")
    return seconds


def loudness(path: Path) -> float:
    """Integrated loudness (LUFS) of a file's sound."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path), "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                       capture_output=True, text=True)
    vals = [ln.split()[1] for ln in r.stderr.splitlines() if ln.strip().startswith("I:")]
    return float(vals[-1]) if vals else -23.0


def cmd_build() -> None:
    sc = load_script()
    plan = json.loads(need(WORK / "voice" / "plan.json", "The narration").read_text())
    tl = json.loads(need(WORK / "rec" / "timeline.json", "The recording").read_text())
    need(WORK / "cards" / "bg.png", "The title cards")
    by_id = {s["id"]: s for s in tl["scenes"]}
    parts, total = [], 0.0
    (WORK / "build").mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(sc["scenes"]):
        out = WORK / "build" / f"{i:02d}-{s['id']}.mp4"
        step(f"Scene {i + 1}/{len(sc['scenes'])}: {s['id']}")
        if s["kind"] == "screen":
            if s["id"] not in by_id:
                sys.exit(f"✗ The recording has no scene '{s['id']}'. Run: demo/make_demo record")
            d = build_screen(s, by_id[s["id"]], plan, out)
        elif s["kind"] == "card":
            d = build_card(s, plan, out)
        else:
            d = build_clip(s, plan, out, opening=(i == 0))
        parts.append(out)
        total += d
    lst = WORK / "build" / "parts.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    full = WORK / "build" / "full.mp4"
    run([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", full])
    OUT.mkdir(parents=True, exist_ok=True)
    final = OUT / "clipping-demo.mp4"
    music = Path(sc.get("music") or "")
    music = music if music.is_absolute() else ROOT / music
    mix = WORK / "build" / "mix.wav"
    if sc.get("music") and music.exists():
        step(f"Mixing in the music ({music.name}), quieter whenever the voice speaks...")
        # tracks are mastered at very different loudness, so set the music relative to the voice, whatever the track:
        # `music_below_voice` dB under it, dipping a little more while someone speaks
        below = float(sc.get("music_below_voice", 14))
        gain = (loudness(full) - below) - loudness(music)
        g = (f"[0:a]asplit=2[v1][v2]; [1:a]aresample=48000,aformat=channel_layouts=stereo,volume={gain:.2f}dB,"
             f"afade=t=in:d=2,afade=t=out:st={max(0, total - 3):.2f}:d=3,atrim=0:{total:.2f}[m]; "
             "[m][v1]sidechaincompress=threshold=0.03:ratio=2:attack=20:release=400[md]; "
             "[v2][md]amix=inputs=2:duration=first:normalize=0[a]")
        run([FFMPEG, "-v", "error", "-y", "-i", full, "-stream_loop", "-1", "-i", music, "-filter_complex", g,
             "-map", "[a]", "-ar", "48000", "-ac", "2", mix])
    else:
        if sc.get("music"):
            print(f"  (no music yet: put a track at {music} and run demo/make_demo build)")
        run([FFMPEG, "-v", "error", "-y", "-i", full, "-map", "0:a", "-ar", "48000", "-ac", "2", mix])
    # loudness in two passes (measure, then set it exactly): -16 LUFS, a good level for YouTube and LinkedIn
    target = "I=-16:TP=-1.5:LRA=11"
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(mix), "-af", f"loudnorm={target}:print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True)
    m = json.loads(r.stderr[r.stderr.rindex("{"): r.stderr.rindex("}") + 1])
    norm = (f"loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
            f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    run([FFMPEG, "-v", "error", "-y", "-i", full, "-i", mix, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
         "-af", norm, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", final])
    shutil.copy(WORK / "cards" / "title.png", OUT / "clipping-demo-poster.png")
    print(f"\n✓ {final}  ({int(total // 60)}:{int(total % 60):02d})")


def main() -> int:
    cmds = sys.argv[1:] or ["all"]
    if cmds == ["all"]:
        cmds = ["voice", "cards", "record", "build"]
    known = {"voices": cmd_voices, "voice": cmd_voice, "cards": cmd_cards, "record": cmd_record, "build": cmd_build}
    for c in cmds:
        if c not in known:
            print(__doc__)
            return 1
    for c in cmds:
        known[c]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
