#!/usr/bin/env python3
"""
Compute the latency that matters for a voice agent — time from the user stopping
speaking to the final transcript arriving — for any capture in the shared
.raw.jsonl schema, whether produced by the engine-only harness or by a
through-jambonz call.

Per utterance (a run of results ending at an is_eos punctuation mark):

  speech_end   = end_time of the last word before the EOS mark (audio timeline)
  final_at     = wall clock when the message carrying that mark was received
  latency      = (final_at - audio_start) - speech_end

audio_start is the RecognitionStarted receipt, which is when audio begins
streaming in both harnesses. Because audio is paced in real time, the audio
timeline and wall clock stay aligned, so the subtraction is meaningful. On a
jambonz capture there is a small extra offset between call answer and first
media; treat single-digit-ms differences as noise.

Comparing the same audio across capture files is the point: it separates engine
latency from what the jambonz media path and gather's turn logic add.

Usage:
  python3 tools/measure-latency.py <capture.raw.jsonl> [more.raw.jsonl ...]
  python3 tools/measure-latency.py --csv out.csv <captures...>
"""

import argparse
import json
import os
import statistics
import sys
from datetime import datetime

PUNCT = {".", ",", "?", "!", "¿", "¡"}


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def content_of(result):
    alternatives = result.get("alternatives") or []
    return alternatives[0].get("content", "") if alternatives else ""


def join_tokens(tokens):
    out = ""
    for tok in tokens:
        if out and tok not in PUNCT:
            out += " "
        out += tok
    return out


def analyze(path):
    audio_start = None
    utterances = []
    current_tokens = []
    last_word_end = None
    config = None
    eou_events = []
    partials = finals = 0
    words = []
    transcript_words = 0

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            msg = entry.get("message", {})
            kind = msg.get("message")
            received = parse_ts(entry.get("received_at"))

            if kind == "RecognitionStarted":
                audio_start = received
                config = msg.get("jambonz_config") or config
                continue

            if kind == "AddPartialTranscript":
                partials += 1
                continue

            if kind == "EndOfUtterance":
                if audio_start and received:
                    eou_events.append((received - audio_start).total_seconds())
                continue

            if kind != "AddTranscript":
                continue

            finals += 1
            transcript_words += len(((msg.get("metadata") or {}).get("transcript") or "").split())
            for result in msg.get("results", []):
                text = content_of(result)
                if not text:
                    continue
                if result.get("type") == "punctuation":
                    current_tokens.append(text)
                    if result.get("is_eos"):
                        if audio_start and received and last_word_end is not None:
                            elapsed = (received - audio_start).total_seconds()
                            utterances.append({
                                "text": join_tokens(current_tokens),
                                "speech_end": last_word_end,
                                "final_at": elapsed,
                                "latency": elapsed - last_word_end,
                            })
                        current_tokens = []
                else:
                    current_tokens.append(text)
                    if result.get("end_time") is not None:
                        last_word_end = result["end_time"]
                    if result.get("start_time") is not None:
                        words.append((result["start_time"], result.get("end_time"), text))

    if current_tokens:
        utterances.append({
            "text": join_tokens(current_tokens),
            "speech_end": last_word_end,
            "final_at": None,
            "latency": None,
        })

    return {
        "path": path,
        "config": config,
        "utterances": utterances,
        "eou_events": eou_events,
        "partials": partials,
        "finals": finals,
        "silence_gaps": silence_gaps(words),
        "word_entries": len(words),
        "transcript_words": transcript_words,
    }


def silence_gaps(words):
    """
    Inter-word silences, longest first. This is what decides whether
    end_of_utterance_silence_trigger can ever fire: if the longest gap in the
    audio is shorter than the trigger, no EndOfUtterance will ever be emitted,
    and with jambonz VAD disabled nothing is left to mark end-of-turn except
    max_delay finalization and punctuation.
    """
    words = sorted(w for w in words if w[1] is not None)
    gaps = []
    for prev, nxt in zip(words, words[1:]):
        gap = nxt[0] - prev[1]
        if gap > 0:
            gaps.append({"gap": gap, "after": prev[2], "before": nxt[2], "at": prev[1]})
    return sorted(gaps, key=lambda g: -g["gap"])


def summarize(report, eou_override=None):
    latencies = [u["latency"] for u in report["utterances"] if u["latency"] is not None]
    name = os.path.basename(report["path"])
    print("=" * 78)
    print(name)
    print("=" * 78)

    eou_trigger = eou_override
    cfg = report.get("config")
    if cfg:
        tc = (cfg.get("speechmaticsOptions") or {}).get("transcription_config", {})
        vad = cfg.get("vad")
        eou_trigger = (tc.get("conversation_config") or {}).get(
            "end_of_utterance_silence_trigger", eou_override)
        print("  config: max_delay=%s/%s  eou=%s  entities=%s  vocab=%d  jambonz_vad=%s" % (
            tc.get("max_delay"), tc.get("max_delay_mode"), eou_trigger,
            tc.get("enable_entities", False),
            len(tc.get("additional_vocab") or []),
            "disabled" if (vad and vad.get("enable") is False) else "default"))

    print("  %d utterances, %d finals, %d partials, %d EndOfUtterance events" % (
        len(report["utterances"]), report["finals"], report["partials"],
        len(report["eou_events"])))

    tw, we = report["transcript_words"], report["word_entries"]
    sparse = tw > 0 and we < 0.8 * tw
    if sparse:
        print()
        print("  !! SPARSE RESULTS: %d word entries for %d words of transcript (%d%%)."
              % (we, tw, round(100.0 * we / tw)))
        print("     The transcript text is complete but the word-level results are not,")
        print("     so silence gaps, speech_end and per-utterance latency below are")
        print("     computed over a partial word list and are NOT reliable. Compare the")
        print("     transcript text instead. This is itself a finding: word timings,")
        print("     confidences and is_eos are being lost before the app sees them.")

    gaps = report["silence_gaps"]
    if gaps and not sparse:
        longest = gaps[0]
        print("  longest silence in audio: %.2fs (after %r at %.2fs)" % (
            longest["gap"], longest["after"], longest["at"]))
        if eou_trigger is not None and not report["eou_events"]:
            if longest["gap"] < eou_trigger:
                print("  >> EOU CANNOT FIRE: trigger %.2fs exceeds the longest silence "
                      "%.2fs." % (eou_trigger, longest["gap"]))
                print("     With jambonz VAD disabled, nothing marks end-of-turn except")
                print("     max_delay finalization and punctuation. Try a trigger below")
                print("     %.2fs, or re-enable VAD." % longest["gap"])
            else:
                print("  >> trigger %.2fs is below the longest silence %.2fs, yet no "
                      "EndOfUtterance arrived — worth investigating."
                      % (eou_trigger, longest["gap"]))
        if len(gaps) > 1:
            print("  next longest: " + ", ".join(
                "%.2fs (after %r)" % (g["gap"], g["after"]) for g in gaps[1:4]))
    print()
    print("   #  speech_end  final_at  latency  text")
    print("  --  ----------  --------  -------  ----")
    for i, u in enumerate(report["utterances"], 1):
        if u["latency"] is None:
            print("  %2d  %10s  %8s  %7s  %s" % (
                i, "%.2fs" % u["speech_end"] if u["speech_end"] else "?",
                "-", "no EOS", u["text"][:44]))
        else:
            print("  %2d  %9.2fs  %7.2fs  %6.2fs  %s" % (
                i, u["speech_end"], u["final_at"], u["latency"], u["text"][:44]))

    if latencies:
        print()
        print("  latency  min %.2fs  median %.2fs  mean %.2fs  max %.2fs" % (
            min(latencies), statistics.median(latencies),
            statistics.fmean(latencies), max(latencies)))
        if min(latencies) < 0.5:
            print("  note: the last utterances are flushed by EndOfStream when the audio")
            print("        file ends, so their sub-0.5s latencies are an artifact of the")
            print("        test, not live behaviour. Judge on the mid-call utterances.")
    print()
    return latencies


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("captures", nargs="+", help="One or more .raw.jsonl capture files")
    parser.add_argument("--csv", help="Also write a per-utterance CSV")
    parser.add_argument("--eou", type=float, default=None,
                        help="end_of_utterance_silence_trigger used for the run, for captures "
                             "that do not record the config (i.e. engine-only harness output)")
    args = parser.parse_args()

    reports = []
    for path in args.captures:
        if not os.path.exists(path):
            sys.exit(f"no such capture: {path}")
        report = analyze(path)
        report["latencies"] = summarize(report, eou_override=args.eou)
        reports.append(report)

    if len(reports) > 1:
        print("=" * 78)
        print("COMPARISON")
        print("=" * 78)
        print("  %-46s %9s %9s" % ("capture", "utts", "median"))
        for report in reports:
            median = (statistics.median(report["latencies"])
                      if report["latencies"] else float("nan"))
            print("  %-46s %9d %8.2fs" % (
                os.path.basename(report["path"])[:46],
                len(report["utterances"]), median))
        counts = {len(r["utterances"]) for r in reports}
        print()
        if len(counts) == 1:
            print("  utterance counts match — no split difference between these runs")
        else:
            print("  UTTERANCE COUNTS DIFFER %s — a split difference, which is the "
                  "signal to chase" % sorted(counts))

    if args.csv:
        with open(args.csv, "w") as f:
            f.write("capture,index,speech_end_s,final_at_s,latency_s,text\n")
            for report in reports:
                base = os.path.basename(report["path"])
                for i, u in enumerate(report["utterances"], 1):
                    text = u["text"].replace('"', "'")
                    f.write('%s,%d,%s,%s,%s,"%s"\n' % (
                        base, i,
                        "" if u["speech_end"] is None else "%.3f" % u["speech_end"],
                        "" if u["final_at"] is None else "%.3f" % u["final_at"],
                        "" if u["latency"] is None else "%.3f" % u["latency"],
                        text))
        print(f"\nCSV written to {args.csv}")


if __name__ == "__main__":
    main()
