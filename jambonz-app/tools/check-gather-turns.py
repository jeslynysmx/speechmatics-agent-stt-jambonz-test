#!/usr/bin/env python3
"""
Audit what the gather verb hands the application, turn by turn.

Checks three things seen to go wrong at turn boundaries:
  - words present in the engine's transcript but missing from every turn
  - spacing artifacts (a space before a punctuation mark)
  - whether each turn restarts the engine's audio timeline, which indicates a
    new recognition session per turn rather than one continuous session

  python3 tools/check-gather-turns.py <gather-capture>.raw.jsonl [reference-capture]

The reference is any capture of the same audio whose transcript is trusted —
normally an engine-only run.
"""
import json
import re
import sys

if len(sys.argv) < 2:
    sys.exit(__doc__)


def transcript_of(path):
    out = []
    for line in open(path):
        m = json.loads(line).get("message", {})
        if m.get("message") != "AddTranscript":
            continue
        t = ((m.get("metadata") or {}).get("transcript") or "").strip()
        if t:
            out.append(t)
    return " ".join(out)


def words(text):
    return re.findall(r"[^\W\d_]+", text.lower(), flags=re.UNICODE)


turns = []
timeline = []
for line in open(sys.argv[1]):
    e = json.loads(line)
    m = e["message"]
    if m.get("message") == "JambonzGatherResult":
        turns.append((e.get("t_offset_ms", 0) / 1000, m.get("turn"), m.get("reason"),
                      ((m.get("metadata") or {}).get("transcript") or "").strip()))
    elif m.get("message") == "AddTranscript":
        starts = [r.get("start_time") for r in m.get("results", [])
                  if r.get("type") == "word" and r.get("start_time") is not None]
        if starts:
            timeline.append(min(starts))

print("=" * 74)
print("GATHER TURNS —", sys.argv[1].split("/")[-1])
print("=" * 74)
for t, n, reason, text in turns:
    print("  turn %s  +%.2fs  (%s)" % (n, t, reason))
    print("    %s" % text)
print()

joined = " ".join(t[3] for t in turns)

# 1. words lost at a boundary
if len(sys.argv) > 2:
    ref = transcript_of(sys.argv[2])
    missing = [w for w in words(ref) if w not in words(joined)]
    print("  reference: %s" % sys.argv[2].split("/")[-1])
    if missing:
        print("  >> WORDS LOST: %s" % missing)
        print("     Present in the engine transcript, absent from every turn — a")
        print("     word dropped where gather closes a turn, not a recognition error.")
    else:
        print("  no words lost against the reference")
else:
    print("  (pass a reference capture as the 2nd argument to check for lost words)")

# 2. spacing artifacts
bad = re.findall(r"\w\s+[.,?!]", joined)
print()
if bad:
    print("  >> SPACING ARTIFACTS: %d occurrence(s), e.g. %r" % (len(bad), bad[:3]))
    print("     A space before a punctuation mark; Speechmatics marks carry")
    print("     attaches_to: previous, so they should be glued to the prior token.")
else:
    print("  no spacing artifacts")

# 3. session restarts
print()
resets = sum(1 for a, b in zip(timeline, timeline[1:]) if b < a - 0.5)
if resets:
    print("  >> TIMELINE RESETS: %d — the engine's audio clock goes backwards, so each"
          % resets)
    print("     turn is a NEW recognition session. Implies a reconnect per turn, with")
    print("     the context of earlier turns lost.")
else:
    print("  audio timeline is monotonic — one continuous session")
