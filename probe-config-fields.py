#!/usr/bin/env python3
"""
Which transcription_config fields does Agent STT actually honour?

The published config list and the deployed preview service do not entirely
agree, and a field that is ignored is ignored silently apart from one Warning
frame at session start — easy to miss, and easy to mistake for a model quirk
later. This opens one session per field and reports what came back.

Nothing is streamed; each session is a StartRecognition and a verdict.

  export SPEECHMATICS_API_KEY=...
  python3 probe-config-fields.py [--host preview.rt.speechmatics.com]
"""
import argparse
import asyncio, json, os, websockets
DEFAULT_HOST="preview.rt.speechmatics.com"
BASE={"language":"es","model":"linden-1","enable_partials":True}
VOCAB=[{"content":"Acme Bistro","sounds_like":["Acme Bistrot"]},
       {"content":"comensales"},{"content":"terraza"}]
CASES=[
 ("additional_vocab",      {"additional_vocab":VOCAB}),
 ("punctuation_overrides", {"punctuation_overrides":{"permitted_marks":[".",",","?","!"],"sensitivity":0.5}}),
 ("domain",                {"domain":"finance"}),
 ("output_locale",         {"output_locale":"es-ES"}),
 ("diarization",           {"diarization":"speaker"}),
 ("baseline (nothing extra)", {}),
]
async def one(url, key, name, extra):
    tc=dict(BASE); tc.update(extra)
    msg={"message":"StartRecognition",
         "audio_format":{"type":"raw","encoding":"pcm_s16le","sample_rate":16000},
         "transcription_config":tc}
    warns=[]
    async with websockets.connect(url, extra_headers={"Authorization":f"Bearer {key}"},
                                  max_size=None, open_timeout=20) as ws:
        await ws.send(json.dumps(msg))
        for _ in range(6):
            d=json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            k=d.get("message")
            if k=="Warning": warns.append(d.get("reason",""))
            if k=="Error": warns.append("ERROR: "+str(d.get("reason") or d)); break
            if k=="RecognitionStarted": break
    rel=[w for w in warns if name.split()[0] in w] if extra else warns
    print("  %-26s %s" % (name, "IGNORED / rejected" if rel else "accepted, no warning"))
    for w in rel: print("      %s" % w)
async def main(url, key):
    print("Agent STT (linden-1) at %s" % url)
    print("which transcription_config fields are honoured:")
    for n,e in CASES:
        try: await one(url,key,n,e)
        except Exception as ex: print("  %-26s FAILED %s" % (n, ex))
        await asyncio.sleep(0.6)

if __name__ == "__main__":
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=DEFAULT_HOST)
    a=ap.parse_args()
    key=os.environ.get("SPEECHMATICS_API_KEY")
    if not key:
        raise SystemExit("set SPEECHMATICS_API_KEY")
    asyncio.run(main(f"wss://{a.host}/v2/agent", key))
