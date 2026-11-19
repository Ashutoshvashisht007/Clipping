# Demo video

Makes a 2–3 minute video of Clipping, with narration, captions and title cards, and no editing by hand.
It drives the real page in Chrome (in a throwaway copy of this folder, so your streams and clips are untouched),
finds moments and makes a clip for real, then puts it all together with ffmpeg.

```
demo/make_demo all       # voice → cards → record the page → build (about 10 minutes)
demo/make_demo build     # rebuild from what's there: after changing the words or adding music (about 1 minute)
demo/make_demo voices    # samples of a few voices, to choose one
```

The video is `demo/out/clipping-demo.mp4`, with a poster image beside it.

## Changing it

- **Words:** edit `say:` in `script.yaml`, then `demo/make_demo voice build`. A line can be `{say: …, show: …}` when it
  should be spoken differently from how it's captioned (e.g. "F F mpeg" / "ffmpeg").
- **Voice:** `demo/make_demo voices`, listen to the files in `demo/out/voices/`, set `voice:` in `script.yaml`, then
  `demo/make_demo voice build`.
- **Music:** put the track at `demo/music.mp3` (or set `music:`), then `demo/make_demo build`. It plays quietly and
  dips further whenever the voice speaks (`music_below_voice:` sets how much quieter than the voice, in dB; any track works, since it is measured first).
- **What's clicked, or the stream used:** `record.js` and `stream:` in `script.yaml`, then `demo/make_demo all`.
- **Title cards:** `cards/cards.html`, then `demo/make_demo cards build`.

## How it works

- `tts.py`: the narration, spoken by Kokoro-82M (Apache-2.0) through mlx-audio, on the Mac. Each line is its own
  file, so captions are timed exactly.
- `record.js`: drives the page with Playwright and records every frame it paints (Chrome's screencast, 1920×1080).
  It draws a cursor and click rings, since automated browsers show none, and notes where each scene starts and
  where the long waits are. Each scene lasts at least as long as its narration.
- `make_demo.py`: builds the throwaway folder and page server, then puts the video together. Long waits (finding
  moments, making the clip) are sped up to a few seconds and labelled "N× speed".

## One-time setup (already done on this Mac)

```
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m venv demo/.venv-voice
demo/.venv-voice/bin/pip install mlx-audio
demo/.venv-voice/bin/pip download misaki==0.9.4 --no-deps -d /tmp/m && demo/.venv-voice/bin/pip install --no-deps --ignore-requires-python /tmp/m/misaki-0.9.4-py3-none-any.whl
demo/.venv-voice/bin/pip install addict regex num2words spacy phonemizer-fork espeakng-loader
demo/.venv-voice/bin/python -m spacy download en_core_web_sm
cd demo && npm install
```

misaki (Kokoro's text-to-sound step) says it needs Python ≤ 3.12 but works on 3.13. Its `[en]` extra is skipped on
purpose: it would pull in PyTorch, which Kokoro on MLX doesn't use.
