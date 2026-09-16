#!/usr/bin/env python3
"""
Speechmatics Agent STT (linden-1) test harness.

Agent STT is a different API from the classic real-time one, not a mode of it.
It emits `AddSegment` — a speaker-attributed transcript string with start/end
times — where the RT API emits `AddTranscript` with word-level `results[]`
carrying `is_eos`, confidences and per-word timings. Tooling written against
the RT API therefore finds nothing here, which is why this is a separate
harness rather than a flag.

Things worth knowing before you compare captures:
  - the endpoint is wss://preview.rt.speechmatics.com/v2/agent, and BOTH the
    /agent path and model=linden-1 are required. Miss either and you silently
    get the classic RT API back, which looks like a bug in your analysis rather
    than your config
  - 16 kHz pcm_s16le only
  - no max_delay / max_delay_mode, no enable_entities, no conversation_config;
    turn-taking is EndOfTurn, not EndOfUtterance
  - it is a preview API, so check what your session actually accepts rather than
    assuming — probe-config-fields.py reports that per field

--check answers one practical question: did a phrase that belongs together stay
inside a single segment? A comma turning into a sentence break is the failure
this was built to detect, and on a turn-segmented API that is a containment
question rather than an is_eos placement one.

Usage:
  export SPEECHMATICS_API_KEY=...
  python3 agent_stt_harness.py --probe                      # handshake only
  python3 agent_stt_harness.py --audio samples/sample-es-reservation.wav
  python3 agent_stt_harness.py --convert <capture>.raw.jsonl
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime

try:
    import websockets
except ImportError:
    sys.exit("Missing dependency. Run: pip install websockets --break-system-packages")

AGENT_URL = "wss://preview.rt.speechmatics.com/v2/agent"
MODEL = "linden-1"

# Default phrase to check for containment: in the bundled Spanish sample this
# sits either side of a comma, so a segment break here means the comma was
# promoted to a sentence boundary. Override with --check-phrase.
DEFAULT_CHECK_PHRASE = "hoy, por favor"


def now():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def build_start(args):
    """
    Only fields Agent STT accepts. Sending an RT-API field such as max_delay or
    enable_entities is not a harmless no-op — it is how you end up debugging a
    rejected StartRecognition instead of reading transcripts.
    """
    tc = {
        "language": args.language,
        "model": MODEL,
        "enable_partials": args.partials,
    }
    if args.permitted_marks:
        tc["punctuation_overrides"] = {
            "permitted_marks": args.permitted_marks,
            "sensitivity": args.punct_sensitivity,
        }
    if args.diarization:
        tc["diarization"] = args.diarization
    if args.domain:
        tc["domain"] = args.domain
    return {
        "message": "StartRecognition",
        "audio_format": {"type": "raw", "encoding": "pcm_s16le",
                         "sample_rate": args.sample_rate},
        "transcription_config": tc,
    }


def ffmpeg_pcm(path, sample_rate):
    """-re paces the stream at real time, as a live call would."""
    return subprocess.Popen(
        ["ffmpeg", "-re", "-i", path, "-f", "s16le", "-acodec", "pcm_s16le",
         "-ar", str(sample_rate), "-ac", "1", "-loglevel", "error", "pipe:1"],
        stdout=subprocess.PIPE)


async def probe(args):
    """
    Does the endpoint exist, does this key reach it, and is linden-1 accepted?
    Connects, sends StartRecognition, waits for the verdict, sends nothing else.
    Answers the question 'is Agent STT actually available to us' without
    committing to a full run.
    """
    print(f"[{now()}] connecting to {AGENT_URL}")
    try:
        async with websockets.connect(
                AGENT_URL, extra_headers={"Authorization": f"Bearer {args.api_key}"},
                max_size=None, open_timeout=20) as ws:
            print(f"[{now()}] websocket OPEN — endpoint exists and the key was accepted")
            start = build_start(args)
            await ws.send(json.dumps(start))
            print(f"[{now()}] sent StartRecognition with model={MODEL!r}")
            for _ in range(5):
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                data = json.loads(raw)
                kind = data.get("message")
                print(f"[{now()}] <- {kind}: {json.dumps(data)[:400]}")
                if kind == "RecognitionStarted":
                    print("\nPROBE PASS — Agent STT is reachable and linden-1 was accepted.")
                    return 0
                if kind == "Error":
                    print("\nPROBE FAIL — the service rejected the session (see Error above).")
                    return 2
    except Exception as err:
        print(f"[{now()}] connection failed: {type(err).__name__}: {err}")
        print("\nPROBE FAIL — could not establish an Agent STT session.")
        return 2
    print("\nPROBE INCONCLUSIVE — no RecognitionStarted and no Error.")
    return 3


async def run_session(args):
    log_lines, raw_messages = [], []

    def log(msg):
        line = f"[{now()}] {msg}"
        print(line)
        log_lines.append(line)

    headers = {"Authorization": f"Bearer {args.api_key}"}
    async with websockets.connect(AGENT_URL, extra_headers=headers,
                                  max_size=None, open_timeout=20) as ws:
        await ws.send(json.dumps(build_start(args)))
        log(f"Sent StartRecognition (model={MODEL}, language={args.language}, "
            f"marks={args.permitted_marks})")
        ready = asyncio.Event()

        async def sender():
            await ready.wait()
            proc = ffmpeg_pcm(args.audio, args.sample_rate)
            seq = 0
            while True:
                chunk = proc.stdout.read(3200)      # ~100ms @ 16k mono s16le
                if not chunk:
                    break
                await ws.send(chunk)
                seq += 1
                await asyncio.sleep(0)
            await ws.send(json.dumps({"message": "EndOfStream", "last_seq_no": seq}))
            log("Sent EndOfStream")

        async def receiver():
            async for raw in ws:
                data = json.loads(raw)
                raw_messages.append({"received_at": datetime.now().isoformat(),
                                     "message": data})
                kind = data.get("message")
                meta = data.get("metadata") or {}
                seg = data.get("segment") or {}
                if kind == "RecognitionStarted":
                    log("RecognitionStarted — audio streaming begins")
                    ready.set()
                elif kind == "AddSegment":
                    who = f" [{seg['speaker']}]" if seg.get("speaker") else ""
                    log(f"SEGMENT{who}: {seg.get('transcript', '')!r}  "
                        f"({meta.get('start_time')}-{meta.get('end_time')}s)")
                elif kind == "AddPartialSegment":
                    if args.partials:
                        log(f"  partial: {seg.get('transcript', '')!r}")
                elif kind in ("SpeechStarted", "SpeechEnded", "StartOfTurn", "EndOfTurn"):
                    t = meta.get("start_time", meta.get("end_time"))
                    log(f"--- {kind} @{t}s ---")
                elif kind == "EndOfTranscript":
                    log("EndOfTranscript — session complete")
                    break
                elif kind in ("Error", "Warning", "Info"):
                    log(f"{kind}: {json.dumps(data)[:300]}")
                    if kind == "Error":
                        break

        ready.clear()
        await asyncio.gather(sender(), receiver())

    base = os.path.basename(args.audio) + ".agent-stt"
    out_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(out_dir, base + ".log.txt"), "w") as f:
        f.write("\n".join(log_lines))
    jsonl = os.path.join(out_dir, base + ".raw.jsonl")
    with open(jsonl, "w") as f:
        for entry in raw_messages:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"\nText log written to {base}.log.txt")
    print(f"Raw JSON written to {base}.raw.jsonl")
    report(jsonl, args.check_phrase)
    return 0


def report(path, check_phrase=DEFAULT_CHECK_PHRASE):
    """Final segments, turn count, and the containment check."""
    segments, turns = [], 0
    for line in open(path):
        m = json.loads(line)["message"]
        if m.get("message") == "AddSegment":
            seg = m.get("segment") or {}
            meta = m.get("metadata") or {}
            segments.append((meta.get("start_time"), meta.get("end_time"),
                             seg.get("speaker"), seg.get("transcript", "")))
        elif m.get("message") == "EndOfTurn":
            turns += 1

    print("\n" + "=" * 70)
    print("SEGMENTS (%d)   turns: %d" % (len(segments), turns))
    print("=" * 70)
    for s, e, spk, text in segments:
        who = f" [{spk}]" if spk else ""
        print(f"  {s}-{e}s{who}  {text}")

    joined = " ".join(t for *_, t in segments)
    print("\n" + "=" * 70)
    print("FULL TRANSCRIPT")
    print("=" * 70)
    print(" ", joined)

    print("\n" + "=" * 70)
    print("CONTAINMENT CHECK — is %r intact inside ONE segment?" % check_phrase)
    print("=" * 70)
    hit = [t for *_, t in segments if check_phrase.lower() in t.lower()]
    if hit:
        print("  PASS — kept whole:", hit[0])
    elif check_phrase.lower() in joined.lower():
        print("  SPLIT ACROSS SEGMENTS — the phrase is present but spans a")
        print("  boundary, i.e. the segmenter broke it where it should not have.")
    else:
        print("  NOT FOUND — the comma is absent or the wording differs.")
        print("  Compare the segments above against your expected transcript.")
    print("  commas in transcript: %d" % joined.count(","))


def main():
    p = argparse.ArgumentParser(description="Speechmatics Agent STT (linden-1) harness")
    p.add_argument("--probe", action="store_true",
                   help="handshake only: is the endpoint reachable and linden-1 accepted")
    p.add_argument("--audio", help="audio file to stream")
    p.add_argument("--convert", help="re-render a saved .raw.jsonl and exit")
    p.add_argument("--api-key", default=None)
    p.add_argument("--language", default="es")
    p.add_argument("--sample-rate", type=int, default=16000,
                   help="Agent STT documents 16 kHz; 8000 is not supported")
    p.add_argument("--permitted-marks", nargs="+", default=[".", ",", "?", "!"])
    p.add_argument("--punct-sensitivity", type=float, default=0.5)
    p.add_argument("--diarization", default=None, choices=[None, "speaker", "none"])
    p.add_argument("--domain", default=None)
    p.add_argument("--check-phrase", default=DEFAULT_CHECK_PHRASE,
                   help="phrase that should stay within a single segment")
    p.add_argument("--partials", action="store_true", default=True)
    p.add_argument("--no-partials", dest="partials", action="store_false")
    args = p.parse_args()

    if args.convert:
        report(args.convert, args.check_phrase)
        return 0

    args.api_key = args.api_key or os.environ.get("SPEECHMATICS_API_KEY")
    if not args.api_key:
        sys.exit("No API key. Use --api-key or set SPEECHMATICS_API_KEY.")

    if args.probe:
        return asyncio.run(probe(args))
    if not args.audio:
        sys.exit("--audio is required (or use --probe / --convert).")
    return asyncio.run(run_session(args))


if __name__ == "__main__":
    sys.exit(main())
