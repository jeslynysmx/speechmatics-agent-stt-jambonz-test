#!/usr/bin/env python3
"""
How many Speechmatics messages did jambonz deliver per webhook, and what did
that cost in latency?

Messages delivered together share a receipt timestamp. The earliest in a batch
waits for the whole batch to fill, so its delivery lags the moment the engine
produced it. Engine-only captures should show one message per webhook.

  python3 tools/analyse-batching.py <capture>.raw.jsonl [more...]
"""
import collections
import json
import os
import sys
from datetime import datetime

if len(sys.argv) < 2:
    sys.exit(__doc__)


def analyse(path):
    t0 = None
    groups = collections.OrderedDict()
    for line in open(path):
        e = json.loads(line)
        m = e["message"]
        ts = datetime.fromisoformat(e["received_at"].replace("Z", "+00:00"))
        if m.get("message") == "RecognitionStarted":
            t0 = ts
            continue
        if m.get("message") != "AddTranscript" or not t0:
            continue
        ends = [r.get("end_time") for r in m.get("results", [])
                if r.get("type") == "word" and r.get("end_time") is not None]
        groups.setdefault(e["received_at"], []).append({
            "recv": (ts - t0).total_seconds(),
            "word_end": max(ends) if ends else None,
            "text": ((m.get("metadata") or {}).get("transcript") or "").strip(),
        })

    sizes = [len(v) for v in groups.values()]
    total = sum(sizes)
    print("=" * 74)
    print(os.path.basename(path))
    print("=" * 74)
    if not total:
        print("  no AddTranscript messages")
        return
    print("  %d messages in %d webhooks   batch sizes: %s"
          % (total, len(groups), collections.Counter(sizes).most_common()))

    held = []
    for items in groups.values():
        if len(items) < 2:
            continue
        print("  batch of %d delivered at %.2fs:" % (len(items), items[0]["recv"]))
        for it in items:
            if it["word_end"] is None:
                print("      %-34s (no word timings)" % it["text"][:34])
                continue
            wait = it["recv"] - it["word_end"]
            held.append(wait)
            print("      %-34s word ended %5.2fs -> held %.2fs"
                  % (it["text"][:34], it["word_end"], wait))
    delays = [it["recv"] - it["word_end"] for items in groups.values()
              for it in items if it["word_end"] is not None]
    if delays:
        print("  word-end -> delivered:  median %.2fs   max %.2fs"
              % (sorted(delays)[len(delays)//2], max(delays)))
    if held:
        print("  worst wait inside a batch: %.2fs" % max(held))
    print()


for p in sys.argv[1:]:
    analyse(p)
