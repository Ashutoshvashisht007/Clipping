"""Speak lines with Kokoro (runs in demo/.venv-voice). Called by make_demo.py.

usage: tts.py <job.json>
job.json = {"voice": "af_heart", "speed": 1.0, "items": [{"text": "...", "out": "/path/line.wav"}, ...]}
Writes each line as 24 kHz mono 16-bit WAV. Lines whose WAV already exists for the same text/voice/speed are skipped.
"""
import hashlib
import json
import sys
import wave
from pathlib import Path

import numpy as np

MODEL = "mlx-community/Kokoro-82M-bf16"


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text())
    voice, speed = job["voice"], float(job.get("speed", 1.0))
    lang = "b" if voice.startswith("b") else "a"  # bf_/bm_ voices are British English
    todo = []
    for it in job["items"]:
        out = Path(it["out"])
        sig = hashlib.sha1(f"{voice}|{speed}|{it['text']}".encode()).hexdigest()
        tag = out.with_suffix(".sig")
        if out.exists() and tag.exists() and tag.read_text() == sig:
            continue
        todo.append((it["text"], out, tag, sig))
    if not todo:
        return 0
    from mlx_audio.tts.utils import load_model
    model = load_model(MODEL)
    for text, out, tag, sig in todo:
        parts, rate = [], 24000
        for r in model.generate(text=text, voice=voice, speed=speed, lang_code=lang):
            parts.append(np.array(r.audio, dtype=np.float32).reshape(-1))
            rate = int(getattr(r, "sample_rate", rate) or rate)
        audio = np.concatenate(parts) if parts else np.zeros(rate // 2, dtype=np.float32)
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())
        tag.write_text(sig)
        print(f"  {out.name}: {len(pcm) / rate:.1f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
