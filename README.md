# Z Scribe for Linux

A native GTK 4/libadwaita media queue and caption review client for Zoom AI
Services Scribe, Translator, and Summarizer. It is the Ubuntu/Linux counterpart
to the [macOS](https://github.com/tanchunsiong/zoom-ai-services-macos-video-management-client)
and [Windows](https://github.com/tanchunsiong/zoom-ai-services-desktop-video-management-client)
clients, with the queue, Live, estimate, and review workflows implemented using
native Linux facilities.

## UI

- **Queue** — drag in media, configure each item's transcription, translation,
  and summary options, start one item or the whole queue, pause/resume between
  items, cancel, retry one/all, search outputs, and inspect event history.
- **Estimates** — see per-job and queue-wide estimated/actual cost and processing
  time. Activity details itemize Scribe, Translator, and Summarizer, and time
  forecasts learn from completed items in the current queue.
- **Review** — preview any queued source, switch original/translated captions,
  play at 1×/1.5×/2×/4×, seek by cue, and read the generated summary. Media that
  GTK cannot decode directly is normalized into a reusable playback cache.
- **Live** — choose a microphone, a PipeWire/PulseAudio system monitor, or both
  simultaneously; select each device independently; view mixed peak
  dBFS/clipping; apply optional software auto gain; translate finalized captions;
  edit vocabulary JSON; copy/clear or summarize the transcript; and open a compact
  caption window. The newest completed speech turn stays at the top.
- **Settings** — save Zoom Build credentials, FFmpeg paths, segment duration,
  Scribe concurrency, account pricing, and character-density overrides.

The processing behavior follows the macOS client: compatible AAC, ALAC, and MP3
audio is copied; other audio is normalized to 128 kbps MP3; uploads target 80 MB;
HTTP 413 and 503 responses trigger smaller segments; and existing sidecars are
reused independently.

## Ubuntu requirements

Ubuntu 24.04 or newer is recommended.

```bash
sudo apt install \
  python3 python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
  gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-libav ffmpeg
```

You also need a Zoom Build API key and secret with Zoom AI Services access.

## Run from the repository

```bash
./scripts/run-dev.sh
```

The script creates a project-local virtual environment while retaining access to
Ubuntu's system PyGObject installation. If `python3-venv` has not been installed,
it still opens the GTK UI directly; Live WebSocket support remains unavailable
until the listed prerequisites are installed.

## Install for the current user

```bash
./scripts/install-local.sh
```

Then open **Z Scribe** from the Ubuntu app grid. Ensure `~/.local/bin` is in your
`PATH` if launching `zscribe-linux` from a terminal.

The installer also registers the bundled Z Scribe icon at standard hicolor sizes
from 32 through 1024 pixels.

## Output and state

Outputs are written beside the source media:

- `meeting.vtt`
- `meeting.translated-zh-CN.vtt`
- `meeting.transcript.json`
- `meeting.summary.md`

XDG locations are used on Linux:

- queue/settings/credentials: `${XDG_DATA_HOME:-~/.local/share}/zscribe`
- temporary work: `${XDG_CACHE_HOME:-~/.cache}/zscribe/work`
- sanitized Live WebSocket diagnostics: `~/.local/share/zscribe/live-diagnostics.log`

`credentials.json` is written atomically with mode `0600`. Authorization headers
and secrets are never written to job events. Temporary audio and request bodies
are deleted after use.

## Live audio notes

Microphone mode uses GStreamer's audio sources. System-audio mode lists the
PulseAudio-compatible monitor nodes exposed by PipeWire (the Linux equivalent of
Windows speaker loopback). Use the refresh button after connecting or changing
an audio device. If no monitor is exposed, ensure an output exists in
`pavucontrol` and that media is actively playing.

**Microphone + system audio** mode captures the selected microphone and loopback
monitor concurrently, resamples both to 16 kHz mono PCM16, and mixes them before
sending one stream to Zoom Live. It does not produce separate speakers or tracks.
Use headphones when possible so desktop audio is not captured acoustically by the
microphone as well as digitally through loopback.

Live source, device, language, translation, vocabulary, and auto-gain choices are
remembered between launches. Auto gain affects only PCM sent to Zoom; it does not
change the desktop volume.

The Live **Summarize** button sends a chronological snapshot of finalized
source-language speech to Zoom Summarizer. Interim words and optional translated
duplicates are excluded. The result and reported input/output character usage are
shown in the expandable Live summary panel.

## Playback cache

Compatibility and playback-speed files live below
`${XDG_CACHE_HOME:-~/.cache}/zscribe/work/playback`. The source remains untouched.
The first selection of a non-1× rate may take a moment while FFmpeg prepares that
rate; subsequent playback reuses the content-keyed cache.

Some Wayland compositors do not allow applications to force a window above all
others. The separate caption window is still independently movable and resizable;
your desktop's “Always on Top” window-menu action can pin it.

## Verify

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q zscribe
desktop-file-validate data/com.tanchunsiong.ZScribeLinux.desktop
```

Automated tests do not contact Zoom. The final end-to-end check is to add one
short media file, save valid credentials, run the queue, and review the generated
VTT. Live transcription likewise requires valid credentials and an audio device.

## Architecture

```text
GTK 4 + libadwaita shell
  |-- XDG persistence (credential file mode 0600)
  |-- GTK/GStreamer playback and cue timeline
  |-- Live transcription
  |    |-- GStreamer PCM16 microphone/system capture
  |    `-- Zoom Scribe Live WebSocket
  `-- MediaPipeline
       |-- FFprobe metadata
       |-- FFmpeg audio extraction/segmentation
       |-- bounded concurrent Zoom Scribe calls
       |-- WebVTT and transcript JSON
       |-- Zoom Translator
       `-- Zoom Summarizer
```

## License

MIT
