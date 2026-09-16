#!/usr/bin/env python3
"""
What did jambonz actually forward on an Agent STT call?

The RT-API tooling reads AddTranscript + results[].
Agent STT sends AddSegment / StartOfTurn / EndOfTurn instead, and we do not yet
know which key jambonz wraps those in — EndOfUtterance has turned up under
`speech_event` rather than `speech.vendor.evt`, so assume nothing.

This prints every distinct message type and payload shape in a capture, then
pulls out anything segment-like. Run it on the first Agent STT call through
jambonz; it tells you what the capture layer needs to understand, rather than
silently finding nothing.

  python3 inspect-jambonz-capture.py <capture>.raw.jsonl
"""
import collections
import json
import sys

if len(sys.argv) < 2:
    sys.exit(__doc__)

kinds = collections.Counter()
shapes = collections.Counter()
segments = []      # AddSegment only — finals
partials = []      # AddPartialSegment — interim, must not be mixed in
unknown = []

for line in open(sys.argv[1]):
    entry = json.loads(line)
    msg = entry.get("message", {})
    kind = msg.get("message") or msg.get("_jambonz_speech_event") or "(none)"
    kinds[kind] += 1
    shapes[tuple(sorted(msg.keys()))[:8]] += 1

    # Agent STT shapes, wherever they turn out to be nested
    def dig(obj, depth=0):
        if depth > 6 or not isinstance(obj, dict):
            return
        if "segment" in obj and isinstance(obj["segment"], dict):
            meta = obj.get("metadata") or {}
            row = (meta.get("start_time"), meta.get("end_time"),
                   obj["segment"].get("speaker"),
                   obj["segment"].get("transcript", ""))
            # A partial is a redraft of the same turn, not another unit of
            # speech. Counting both gives ~4x the real segment count and a
            # nonsense transcript, so keep them apart.
            (partials if obj.get("message") == "AddPartialSegment"
             else segments).append(row)
        for v in obj.values():
            if isinstance(v, dict):
                dig(v, depth + 1)
            elif isinstance(v, list):
                for item in v:
                    dig(item, depth + 1)
    dig(msg)

    if msg.get("_synthesized_from_jambonz_payload"):
        unknown.append(entry)

print("=" * 72)
print("MESSAGE TYPES —", sys.argv[1].split("/")[-1])
print("=" * 72)
for kind, n in kinds.most_common():
    print("  %-34s %d" % (kind, n))

print("\n" + "=" * 72)
print("PAYLOAD KEY SHAPES")
print("=" * 72)
for keys, n in shapes.most_common(10):
    print("  %-56s %d" % (", ".join(keys)[:56], n))

print("\n" + "=" * 72)
print("FINAL SEGMENTS (%d)   [%d partials not shown]" % (len(segments), len(partials)))
print("=" * 72)
if segments:
    for s, e, spk, text in segments:
        print("  %s-%ss [%s]  %s" % (s, e, spk or "?", text))
    joined = " ".join(t for *_, t in segments)
    print("\n  full: %s" % joined)
    print("  commas: %d" % joined.count(","))
    whole = [t for *_, t in segments if "hoy, por favor" in t.lower()]
    print("  'hoy, por favor' whole in one segment: %s" % ("YES" if whole else "NO"))
else:
    print("  none — jambonz did not forward AddSegment in any shape we recognise.")

if unknown:
    print("\n" + "=" * 72)
    print("UNRECOGNISED PAYLOADS (%d) — the capture layer kept them verbatim" % len(unknown))
    print("=" * 72)
    for entry in unknown[:3]:
        print("  " + json.dumps(entry.get("message", {}).get("_jambonz_payload", {}))[:500])
    print("\n  Send these to jambonz if the segments above are empty — they show")
    print("  exactly what arrived and under which key.")
