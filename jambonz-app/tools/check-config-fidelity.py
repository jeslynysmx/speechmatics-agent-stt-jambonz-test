#!/usr/bin/env python3
"""
Did the recognizer config survive the trip to the engine?

The capture records both halves: the synthetic RecognitionStarted entry holds
`jambonz_config` — what the app asked for — and the AddTranscript results hold
what the engine actually produced. Comparing them detects a config altered in
transit without needing the provider's logs.

The sharpest probe is punctuation. Ask for one mark only; if a different mark
comes back, something rewrote the request.

  python3 tools/check-config-fidelity.py <capture>.raw.jsonl
"""
import collections
import json
import sys

if len(sys.argv) < 2:
    sys.exit(__doc__)

requested = None
marks = collections.Counter()
eos = 0
words = 0

for line in open(sys.argv[1]):
    entry = json.loads(line)
    msg = entry.get("message", {})
    if msg.get("message") == "RecognitionStarted":
        cfg = msg.get("jambonz_config") or {}
        tc = (cfg.get("speechmaticsOptions") or {}).get("transcription_config", {})
        requested = tc
        continue
    if msg.get("message") != "AddTranscript":
        continue
    for r in msg.get("results", []):
        content = ((r.get("alternatives") or [{}])[0]).get("content", "")
        if r.get("type") == "punctuation":
            marks[content] += 1
            if r.get("is_eos"):
                eos += 1
        elif r.get("type") == "word":
            words += 1

print("=" * 70)
print("CONFIG FIDELITY —", sys.argv[1].split("/")[-1])
print("=" * 70)

asked = None
if requested:
    po = requested.get("punctuation_overrides") or {}
    asked = po.get("permitted_marks")
    print("  app requested:")
    print("    permitted_marks     %s" % asked)
    print("    sensitivity         %s" % po.get("sensitivity"))
    print("    max_delay / mode    %s / %s" % (requested.get("max_delay"),
                                               requested.get("max_delay_mode")))
    print("    operating_point     %s" % requested.get("operating_point"))
    print("    enable_entities     %s" % requested.get("enable_entities"))
    print("    eou trigger         %s" % (requested.get("conversation_config") or {})
          .get("end_of_utterance_silence_trigger"))
    print("    additional_vocab    %d entries" % len(requested.get("additional_vocab") or []))
else:
    print("  (no jambonz_config in this capture — engine-only run?)")

print()
print("  engine produced:")
print("    word entries        %d" % words)
print("    punctuation marks   %s" % (dict(marks) or "none"))
print("    is_eos marks        %d" % eos)

if asked is not None:
    got = set(marks)
    unrequested = sorted(got - set(asked))
    unused = sorted(set(asked) - got)
    print()
    if unrequested:
        print("  >> CONFIG ALTERED IN TRANSIT")
        print("     marks %s came back but were NOT in permitted_marks." % unrequested)
        print("     The engine cannot emit a mark it was not permitted, so the")
        print("     request that reached it differed from the one the app sent.")
    else:
        print("  >> permitted_marks appears to have survived: every mark produced")
        print("     (%s) was requested." % (sorted(got) or "none"))
    if unused:
        print("     (requested but unused, which is normal if the audio has no %s: %s)"
              % ("/".join(unused), unused))
