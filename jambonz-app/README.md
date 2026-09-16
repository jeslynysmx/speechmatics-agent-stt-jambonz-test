# Testing Speechmatics through jambonz

A jambonz websocket application plus a SIP injector, so the same audio can be
pushed through a **real SIP call** into jambonz and out to Speechmatics, and the
result compared against the engine-only capture in `../samples/`.

That comparison is the point. Three places a transcript can change shape:

```
engine only          ->   /transcribe        ->   /gather
(no jambonz at all)       (jambonz media          (jambonz media path +
                           path only)              gather's turn logic)
```

Run the same clip through all three and whichever hop changes the output is the
one to look at. The engine-only leg is the parent directory's harness.

Works with both the classic RT API and Agent STT — Agent STT is selected by
vendor, see below.

## Requirements

- Node 18+, `npm`
- Python 3 with `websockets` (for the analysis tools)
- `sipp` — `apt-get install sip-tester`
- `ffmpeg`
- a tunnel such as `ngrok`, since jambonz must reach your app over the internet
- a jambonz account you can configure

## 1. Set up jambonz

In the portal:

1. **Speech → add credential.** For the classic RT API, vendor `Speechmatics`.
   For Agent STT, vendor **`Speechmatics Agent`**. Give it a Label if you will
   run both — the app passes the label through, and without one jambonz looks
   for an unlabelled credential of that vendor and fails with no obvious error.
2. **Applications → add.** Calling webhook and call status webhook both
   `wss://<your-tunnel-host>/transcribe` (or `/gather`). Two applications, one
   per route, is easier than editing one back and forth.
3. **Account → SIP realm.** Note it for `.env.injector`.
4. **Account → Application for SIP device calls.** Point it at the application
   from step 2. This is what routes a call from a registered SIP user to your
   app, with no phone number or carrier needed. It is account-wide, so two
   people testing the same account at once will steal each other's calls.
5. **Clients → add.** A SIP username and password for the injector.

## 2. Run the app

```bash
npm install
WS_PORT=3010 npm start
ngrok http 3010          # use the https host as wss://<host>/transcribe
```

ngrok's hostname changes on every restart and the jambonz application keeps
pointing at the dead one — the call then fails with `X-Reason: 404 Not Found`,
which looks like an app bug but is not. `inject-call.sh` prints the live tunnel
on every run so you can catch the mismatch before the call.

## 3. Check the verbs before spending a call

```bash
node tools/replay-mock-jambonz.js --url ws://localhost:3010/transcribe
```

Replays a saved capture through the app as if jambonz had delivered it and
prints the exact verbs your app sends. Confirm the recognizer block says what
you meant before placing a real call. It exercises your route code and config
only — it says nothing about how jambonz behaves.

## 4. Inject a call

```bash
cp .env.injector.example .env.injector   # then fill it in
chmod 600 .env.injector
./tools/inject-call.sh
```

It converts the wav to a G.711 RTP pcap, REGISTERs the SIP user, dials, replays
the pcap as call media, and hangs up after the audio plus a finalization tail.
Captures land in `captures/` as `.raw.jsonl`.

## 5. Analyse

```bash
# Agent STT: what came back, and did phrases stay whole
python3 ../inspect-jambonz-capture.py "$(ls -t captures/*.raw.jsonl | head -1)"

# classic RT API
python3 tools/check-config-fidelity.py  <capture>    # did config survive the trip
python3 tools/measure-latency.py        <capture>    # speech end -> final arrival
python3 tools/analyse-batching.py       <capture>    # messages per webhook
python3 tools/check-gather-turns.py     <capture>    # lost words, spacing, timeline
```

`check-config-fidelity.py` is the useful one for arguing about config: the
capture records both what the app asked for and what the engine produced, so a
request altered in transit shows up without needing the provider's logs.

## Configuring the recognizer

`lib/recognizer-config.js` holds the defaults. Two ways to override without
touching code:

- environment variables, read at startup (`SM_LANGUAGE`, `SM_MAX_DELAY`,
  `SM_EOU_SILENCE`, `SM_PERMITTED_MARKS`, `JAMBONZ_VAD`, …)
- `config/recognizer-overrides.json`, **re-read on every call**, so a parameter
  sweep needs no restart. See `recognizer-overrides.example.json`.

### Agent STT

Agent STT is a separate vendor, not a model field — sending `model` inside
`transcription_config` is rejected client-side by `@jambonz/verb-specifications`
as an unknown property. Select it with:

```json
{"agentMode": true, "vendor": "speechmaticsagent", "label": "<your label>"}
```

`agentMode` also strips the fields Agent STT does not accept — `max_delay`,
`max_delay_mode`, `enable_entities`, `conversation_config` — because sending one
is not a harmless no-op, it is a rejected StartRecognition.

To read the exact vendor string for your own account:

```
GET /api/v1/Accounts/<account_sid>/SpeechCredentials
```

with a jambonz API key (not your Speechmatics key — that returns 401 and an HTML
body, which surfaces as a confusing JSON parse error).

## Validating the injector without a jambonz account

```bash
python3 tools/fake-sip-uas.py &
sipp 127.0.0.1:5080 -sf tools/sipp/inject-audio.xml -i 127.0.0.1 -p 5070 \
  -min_rtp_port 6010 -max_rtp_port 6020 -s 1000 -inf users.csv \
  -key realm 127.0.0.1 -key pcap captures/<your>.pcap \
  -d 18000 -m 1 -au testuser -ap secret
```

A local stand-in SBC that challenges with digest auth, records the RTP and
reports what arrived — packet count, sequence gaps, and **pacing**. The pacing
line matters: each packet carries 20ms of audio, so a sender true to the clock
delivers them 20ms apart and the stream lasts exactly as long as the audio. A
slow sender stretches it, and a recognizer downstream then sees longer silences
than the audio contains, which moves punctuation and end-of-sentence decisions.
Check this before blaming a provider for a segmentation difference.

## Gotchas

- `node-client-ws` pins `@jambonz/verb-specifications ^0.0.121`, whose validator
  rejects `conversation_config` and the newer `host`/`profile` options, and
  `^0.0.x` never resolves to `0.1.x`. `package.json` carries an `overrides` entry
  forcing `^0.1.13`. Client-side only — the feature server still has to support
  the field.
- `sipp`'s `[authentication]` emits its own header name, so prefixing it
  duplicates the header and the credentials are rejected.
- `-mp` does not exist in sipp 3.7.2; media ports come from `-min_rtp_port` /
  `-max_rtp_port`.
- Trailing utterances are flushed when the audio ends, so their latencies look
  artificially low. Judge on mid-call utterances.
- Speech events do not all arrive under the same key — transcripts come under
  `speech.vendor.evt`, while `EndOfUtterance` has been seen under `speech_event`.
  `lib/capture.js` reads both and preserves anything it does not recognise, so
  an unfamiliar payload shows up in the capture rather than vanishing.
