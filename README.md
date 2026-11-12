# Clipping

Find the best moments in a stream, then turn them into 1080×1920 clips with word-by-word captions.
Everything runs on your Mac. The only optional paid step is the AI ranking (about $0.003 per stream on OpenCode Go).

## The page (easiest)

Double-click **Clipping.command** in this folder (or run `bin/clipui`). A Terminal window opens and the page opens in
your browser at http://localhost:8765. Keep that Terminal window open while you work; close it to stop.

- **Find moments:** paste a stream link and press Find moments. Click a moment to watch it right there, set the start
  and end (±1 s buttons, or "Start/End = video's time now"), add a hook and press **Make clip**.
  A single Twitch clip link works too: it goes straight to making the clip.
- **Clips:** every clip with its YouTube title, YouTube description and Instagram caption (edit, then Copy), the
  before-posting checklist, and, once posted, the links and views that go into `log.csv`.
  - **Edit captions** fixes misheard words in the video's captions and re-makes the clip (about 30 s).
    "Replace everywhere" + "Always" adds the fix to `fixes.yaml`, so that streamer's new clips come out right.
  - **Write with AI** (re)writes the three post texts. New clips get them automatically (`clips.ai_text` in config.yaml).
- **The AI pauses itself** when it's unreachable, refuses the key or the usage limit is reached: until it's back,
  moments are ranked by chat and volume, and clips get the plain title and hashtags. It tries again by itself (after
  5 minutes, or 1 hour for the limit, then longer); "Try AI now" in Streamers & setup tries straight away.
- **Your logo:** every new clip gets your logo (`brand/`), cut out of its background automatically.
  Streamers & setup → "Your logo on every clip" sets where it goes and how big, changes the logo, and updates
  existing clips to match (Terminal: `clip brand logo <picture>`, `clip brand apply`). Clips the logo was added to
  afterwards keep a logo-free copy in `inbox/_unbranded/`, so moving or removing the logo never doubles it.
- **Streamers & setup:** record each streamer's permission, set their camera box by dragging on a frame, add
  streamers, record a live stream, and check that the tools, caption model and AI key are found.

The page only listens on this Mac. It runs the same commands as below, so everything still works from Terminal too.

## Every day (Terminal)

1. **Find moments.** `findmoments <Twitch VOD / YouTube / Kick link>`
   This writes `moments/<streamer>/<date>-….md`. Open it, watch each moment with its link, and keep the good ones.
   *Shortcut:* on the streamer's Twitch page, go to **Videos → Clips → Top 24H** and use a clip link directly in step 2.
2. **Make the clip.** Copy the `makeclip …` line from the moments file into Terminal,
   or run `makeclip <clip link> <streamer>`.
   You get `out/<date>/<streamer>-<time>.mp4`, plus a `.txt` file with the title, credit, hashtags and a checklist.
3. **Finish.** Watch it on your phone and tick the checklist. Fix anything in Kdenlive if needed.
4. **Post** on YouTube Shorts and Instagram Reels, then paste both links into `log.csv`.
5. **Every Sunday**, fill in views, % watched, follows and shares in `log.csv`.
   Then do more of whatever brings **follows**.

## One-time setup

- **Use the commands from any folder.** Run this once, then open a new Terminal window:
  `echo 'export PATH="$HOME/Developer/Clipping/bin:$PATH"' >> ~/.zshrc`
- **Permission.** For each streamer in `streamers.yaml`, set `permission.source` and `permission.proof`.
  `makeclip` refuses streamers without them. Add `--practice` for clips you won't post.
- **Camera position.** Run `clip camera <streamer> <stream link> <time when the camera is visible>`, drag a box
  around the camera, then press Enter. A preview opens. For a streamer with no camera, use `clip camera <streamer> --none`.
- **Add your OpenCode Go key.** Run `security add-generic-password -a "$USER" -s opencode-go -w`, then paste the key
  when asked (it won't show on screen). It's stored in your Mac's Keychain, not in any file.
  A line `opencode_go_key=…` in this folder's `.env` file works too, but that file is plain text: never share or zip
  the folder with it inside.
  To turn the AI step off, set `rank.provider: none` in `config.yaml`.
- **Check everything:** `clip check`

## Commands

| Command | What it does |
|---|---|
| `findmoments <link>` | Finds chat and volume spikes, drops loud moments with nobody talking, transcribes the rest, and ranks them (AI, if a key is set). |
| `findmoments <link> --from 4:00:00` | Analyses only part of a stream, e.g. the last few hours. `--to` works too. |
| `findmoments <link> --full-audio` | Downloads the whole audio, so volume spikes count even where chat was quiet. Slower. |
| `makeclip <link> <start> <end> <streamer>` | Makes the vertical clip. Options: `--hook "text"`, `--title "text"`, `--layout full`, `--keep-silence`, `--practice`, `--again`. |
| `clip camera <streamer> <link> <time>` | Sets where the camera sits. You can also give `--box X,Y,W,H` in pixels instead of dragging. |
| `clip record <streamer>` | Records a live stream to `inbox/live/`. Then run `findmoments <file> --streamer <name>` (volume only, no chat). |
| `clip check` | Checks tools, the caption model, the AI key, permissions and camera boxes. |

Times can be written `1:23:45`, `83:45`, `5025` or `1h23m45s`.

## Where things are

| Path | What |
|---|---|
| `streamers.yaml` | Who you clip: channel, credit handle, permission proof |
| `config.yaml` | AI model, caption style, hashtags, silence cutting |
| `moments/` | One moments file per analysed stream |
| `out/<date>/` | Finished clips + caption text (`out/practice/` for practice clips) |
| `log.csv` | One row per clip, for your weekly review |
| `state.json` | What's already been analysed or clipped, so nothing is done twice |
| `inbox/` | Downloaded pieces. Deleted automatically after 7 days |
| `layouts/` | Camera boxes and previews |

## Rules built in

- No permission proof means no clip, except practice clips.
- Moments the AI thinks spoil GTA 6's story are flagged. Don't post them.
- If a clip overlaps one you've already made from the same stream, makeclip stops. Add `--again` to make it anyway.
