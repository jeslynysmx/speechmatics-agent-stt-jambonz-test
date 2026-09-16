# Speechmatics Agent STT — test harness

A small rig for exercising Speechmatics' **Agent STT** API (`model: linden-1`,
`wss://preview.rt.speechmatics.com/v2/agent`), both on its own and through a
telephony stack such as jambonz, so the two can be compared on identical audio.

Agent STT is a **different API** from the classic real-time one, not a mode of
it. It emits `AddSegment` — a speaker-attributed transcript string with start
and end times — where the RT API emits `AddTranscript` carrying word-level
`results[]` with `is_eos`, per-word confidences and timings. Tooling written
against the RT API finds nothing here, which is the reason this exists.

Two things are required together, and missing either gives you the **classic RT
API back with no error**: the `/v2/agent` path, and `model: "linden-1"` in
`transcription_config`. That failure mode is quiet and costs an afternoon, so
`--probe` checks it explicitly.

## Two legs

| | |
| --- | --- |
| **This directory** | Straight to the Speechmatics API, no telephony. Needs only your API key, Python and ffmpeg. Start here — it gives you the control to compare everything else against. |
| [`jambonz-app/`](jambonz-app/) | A jambonz websocket app and a SIP injector, so the same audio goes through a real SIP call into jambonz and out to Speechmatics. Needs a jambonz account you can configure, plus sipp and a tunnel. |

Running both on the same clip is what makes a difference attributable: if the
transcript changes between the two, the telephony path did it.

## Quick start

```bash
pip install websockets          # add --break-system-packages on Debian/Ubuntu
export SPEECHMATICS_API_KEY=...

# 1. is the endpoint reachable, and is linden-1 accepted?
python3 agent_stt_harness.py --probe

# 2. stream the sample clip and see the segments
python3 agent_stt_harness.py --audio samples/sample-es-reservation.wav --no-partials
```

`ffmpeg` must be on PATH — it transcodes and paces the audio at real time
(`-re`), because finalization timing depends on the audio arriving at the speed
a live call would deliver it.

## The tools

| | |
| --- | --- |
| `agent_stt_harness.py` | Streams audio to Agent STT and captures every message verbatim to `.raw.jsonl`, plus a readable log. `--probe` for a handshake-only check, `--convert` to re-render a saved capture without touching the API. |
| `probe-config-fields.py` | Opens one session per `transcription_config` field and reports which the service accepts. A field it does not support is reported once, in a `Warning` frame at session start, and then ignored silently — easy to miss, and easy to mistake for a model quirk later. |
| `inspect-jambonz-capture.py` | Reads a capture taken from a telephony stack, prints every message type and payload shape, and digs out segments wherever they are nested. Use it when you do not yet know what shape the integration forwards. |

## Sample

`samples/sample-es-reservation.wav` is a 14.8s synthesized Spanish restaurant
booking. Because it is TTS, the intended text is known exactly, which makes it
usable as ground truth — in particular it contains two commas, and whether those
survive as commas or get promoted to sentence boundaries is the thing this rig
was built to measure.

`samples/sample-es-reservation.agent-stt.raw.jsonl` is a capture of that clip
sent straight to Agent STT with no telephony in the path — the control to diff
against. It renders as seven segments:

```
python3 agent_stt_harness.py --convert samples/sample-es-reservation.agent-stt.raw.jsonl
```

```
0.00-2.32s  [S1]  Quiero una mesa para hoy, por favor.
2.58-3.42s  [S1]  A las ocho.
3.74-5.82s  [S1]  Seremos 4,1 bebé.
6.30-6.90s  [S1]  Si.
7.02-9.50s  [S1]  Hay un vegano y un intolerante a la lactosa.
9.58-11.38s [S1]  A nombre de Serge Prieto.
11.62-14.70s[S1]  Sí, correcto. Muchas gracias. Hasta luego.
```

Note the granularity: segments are **turns, not sentences**. The last one holds
three sentences. If your downstream consumes sentences, that is a design change,
not a drop-in.

## Through jambonz

jambonz exposes Agent STT as a **separate vendor**, not as a `model` field —
sending `model` inside `transcription_config` is rejected client-side by
`@jambonz/verb-specifications` (`unknown property model`). Add the credential in
the portal under Speech, then reference it by vendor and label:

```json
{
  "vendor": "speechmaticsagent",
  "label": "<your credential label>",
  "speechmaticsOptions": {
    "transcription_config": {"language": "es", "enable_partials": true}
  }
}
```

The vendor string is one word. To read it back for your own account:

```
GET /api/v1/Accounts/<account_sid>/SpeechCredentials
```

Turn signals reach the application: `EndOfTurn` is surfaced as an
`EndOfUtterance` speech event. Note that it may arrive under a different key
than vendor transcript events do, so capture the whole payload rather than one
key — `inspect-jambonz-capture.py` exists because of exactly that.

## Licence

MIT.
