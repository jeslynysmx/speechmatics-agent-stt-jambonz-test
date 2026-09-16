# Testing Speechmatics through jambonz

A jambonz websocket application plus a SIP injector, so the same audio goes
through a **real SIP call** into jambonz and out to Speechmatics, and the result
can be compared against the engine-only capture in `../samples/`.

That comparison is the point. There are three places a transcript can change
shape, and running one clip through all three tells you which hop did it:

```
engine only          ->   /transcribe        ->   /gather
(no jambonz at all)       (jambonz media          (jambonz media path +
                           path only)              gather's turn logic)
```

Set up for **Agent STT**, which needs very little: a language and whether you
want partials. The classic RT API is also supported, for when you want to
compare the two on the same audio — it takes a lot more configuration, and
`lib/recognizer-config.js` holds those defaults.

## You need

- a jambonz account you can configure. If your team shares one for testing, get
  the sign-in from whoever owns it — it is deliberately not in this repo
- Node 18+, Python 3, `ffmpeg`
- `sipp` — `apt-get install sip-tester`
- a tunnel such as `ngrok`, because jambonz has to reach your app over the
  internet

Everything below is the sequence that actually works, in order. The portal steps
and the terminal steps interleave; doing them out of order mostly produces calls
that connect and transcribe nothing.

---

## 1. Start the app and the tunnel

Do this first, because the tunnel hostname goes into the portal in step 3.

```bash
npm install
WS_PORT=3010 npm start
```

In a second terminal:

```bash
ngrok http 3010
```

Note the https URL it prints, e.g. `https://abc123.ngrok-free.dev`. The webhook
form of it is `wss://abc123.ngrok-free.dev/transcribe`.

## 2. Add the speech credential

Portal → **Speech** → add credential.

- For the classic RT API, vendor **Speechmatics**.
- For Agent STT, vendor **Speechmatics Agent**.

Give it a **Label** (e.g. `AgentSTT`) if you will run both, and remember it —
the app passes the label through, and jambonz matches on vendor *and* label. A
credential with a label that your config does not name will not be found, and
the call fails with nothing obvious in the app log.

## 3. Create the application

Portal → **Applications** → add.

Set **both** the calling webhook and the call status webhook to:

```
wss://<your-ngrok-host>/transcribe
```

Making a second application pointing at `/gather` is worth it — switching
between two applications is faster and less error-prone than editing one
webhook back and forth.

## 4. Route SIP calls to it

Portal → **Account** → **Application for SIP device calls** → select the
application from step 3.

This is what makes a call from a registered SIP user reach your app, with no
phone number or carrier involved. Note that it is **account-wide**: if two
people test on the same account at once, whoever set it last receives both
their calls. Coordinate, or use separate accounts.

While you are on the Account page, note the **SIP realm** for step 6.

## 5. Add a SIP client

Portal → **Clients** → add a username and password. This is what the injector
registers as. If you are sharing an account, make your own client rather than
sharing one — concurrent registrations of the same user fight each other.

## 6. Configure the injector

```bash
cp .env.injector.example .env.injector
chmod 600 .env.injector
```

Fill in `SIP_REALM` (from step 4), `SIP_USER` and `SIP_PASS` (step 5). `SBC_HOST`
is where the SIP packets actually go — on some clusters the per-account realm
does not resolve in DNS and only the cluster SBC hostname does, which is why it
is a separate setting.

`.env.injector` is gitignored. Keep it that way.

## 7. Choose the recognizer config

`config/recognizer-overrides.json` is re-read **on every call**, so you can
change parameters between calls without restarting the app.

For Agent STT that is the whole config — use your own label from step 2:

```bash
echo '{"agentMode":true,"vendor":"speechmaticsagent","label":"AgentSTT"}' \
  > config/recognizer-overrides.json
```

There is deliberately nothing else in there. Agent STT takes a language and
`enable_partials`, which the app already sets, and `agentMode` strips the RT-API
fields it does not accept — `max_delay`, `max_delay_mode`, `enable_entities`,
`conversation_config`. Sending one of those is not a harmless no-op; it is a
rejected StartRecognition. Turn-taking is the model's job here, so there is no
`max_delay` to tune.

<details>
<summary>Classic RT API instead, for comparison</summary>

Different vendor, and a lot more knobs — this is a production-shaped example:

```bash
echo '{"maxDelay":0.7,"maxDelayMode":"flexible","eouSilence":0.7,
       "permittedMarks":[".",",","?","!"],"enableEntities":true}' \
  > config/recognizer-overrides.json
```

</details>

**Get the vendor string from your own account rather than copying it.** With a
**jambonz** API key (Portal → Account → API keys — *not* your Speechmatics key):

```bash
curl -s -w '\nHTTP %{http_code}\n' \
  -H "Authorization: Bearer <JAMBONZ-api-key>" \
  https://<your-jambonz-host>/api/v1/Accounts/<account_sid>/SpeechCredentials
```

Find the entry with your label and read its `vendor` field verbatim.

## 8. Check the verbs before spending a call

```bash
node tools/replay-mock-jambonz.js --url ws://localhost:3010/transcribe
```

This replays a saved capture through your app as if jambonz had delivered it,
and prints the exact verbs your app sends. Read the `recognizer` block and
confirm it says what you meant — particularly `vendor` and `label`.

Thirty seconds here saves a wasted call. A placeholder left in the overrides
file produces a call that connects, transcribes nothing, and looks like a
jambonz fault.

## 9. Place the call

```bash
./tools/inject-call.sh
```

It converts the wav to a G.711 RTP pcap, REGISTERs the SIP user, dials, replays
the pcap as call media, and hangs up after the audio plus a finalization tail.

It prints the live tunnel URL first — check it matches what you put in the
portal at step 3. At the end it prints `requests seen by the tunnel`. If that is
`0`, jambonz never reached you and the webhook is wrong.

Captures land in `captures/` as `.raw.jsonl`.

## 10. Read the result

```bash
python3 ../inspect-jambonz-capture.py "$(ls -t captures/*.raw.jsonl | head -1)"
```

It prints every message type in the capture and the final segments. A working
Agent STT call shows `AddSegment`, `StartOfTurn` and `EndOfTurn`; compare the
segments against `../samples/sample-es-reservation.agent-stt.raw.jsonl`, which
is the same clip with no telephony in the path. Segments that match mean the
call added nothing of its own.

The app log is the fastest signal: a healthy call ends with non-zero `counts`.
`{"partial":0,"final":0,"eou":0}` means the recognizer never produced anything,
and the cause is almost always step 7.

<details>
<summary>Analysing a classic RT API capture</summary>

The RT API carries word-level `results[]` with `is_eos`, so more is measurable:

```bash
CAP=$(ls -t captures/*.raw.jsonl | head -1)
python3 tools/check-config-fidelity.py "$CAP"   # did the config survive the trip
python3 tools/measure-latency.py "$CAP"         # speech end -> final arrival
python3 tools/analyse-batching.py "$CAP"        # messages per webhook
python3 tools/check-gather-turns.py "$CAP"      # lost words, spacing, timeline
```

`check-config-fidelity.py` is the one to reach for in a disagreement about
config: the capture records both what the app asked for and what the engine
produced, so a request altered in transit is visible without the provider's
logs.

</details>

---

## When it does not work

**The call connects but nothing is transcribed** — `counts` all zero. The
vendor or label in `config/recognizer-overrides.json` does not match a
credential on the account. Re-run step 8, then check the vendor string against
the API as in step 7.

**`X-Reason: 404 Not Found` on the call.** The application's webhook names a
dead ngrok host. ngrok's hostname changes on every restart and the portal keeps
pointing at the old one. Update both webhooks.

**`Expecting value: line 1 column 1` from the jambonz API.** You sent your
Speechmatics key where a jambonz API key was wanted. It returns 401 with an HTML
body, which surfaces as a JSON parse error rather than an auth error.

**Someone else's transcripts appear, or yours vanish.** Two testers are sharing
one account's "Application for SIP device calls" (step 4).

**The recognizer config change had no effect.** Env vars are read at startup;
only `config/recognizer-overrides.json` is re-read per call. Anything in
`lib/` needs a restart.

## Checking your injector's pacing

If a transcript differs between the engine-only run and the call, and you
suspect the difference is timing rather than configuration, check your own
sender before blaming anything downstream.

```bash
python3 tools/fake-sip-uas.py &
sipp 127.0.0.1:5080 -sf tools/sipp/inject-audio.xml -i 127.0.0.1 -p 5070 \
  -min_rtp_port 6010 -max_rtp_port 6020 -s 1000 -inf captures/sip-user.csv \
  -key realm 127.0.0.1 -key pcap captures/<your>.pcap \
  -d 18000 -m 1 -au <user> -ap secret
```

A local stand-in SBC that challenges with digest auth, records the RTP and
reports packet count, sequence gaps and **pacing**. Each packet carries 20ms of
audio, so a sender true to the clock delivers them 20ms apart and the stream
lasts exactly as long as the audio. A slow sender stretches it, and a recognizer
downstream then sees longer silences than the audio actually contains — which
moves punctuation and end-of-sentence decisions. That is a real way to
manufacture a segmentation difference that has nothing to do with the provider.

## Other gotchas

- `node-client-ws` pins `@jambonz/verb-specifications ^0.0.121`, whose validator
  rejects `conversation_config` and the newer `host`/`profile` options, and
  `^0.0.x` never resolves to `0.1.x`. `package.json` carries an `overrides` entry
  forcing `^0.1.13`. That is client-side only — the feature server still has to
  support the field.
- `sipp`'s `[authentication]` emits its own header name, so prefixing it
  duplicates the header and the credentials are rejected.
- `-mp` does not exist in sipp 3.7.2; media ports come from `-min_rtp_port` /
  `-max_rtp_port`.
- Trailing utterances get flushed when the audio ends, so their latencies look
  artificially low. Judge on mid-call utterances.
- Speech events do not all arrive under the same key — transcripts come under
  `speech.vendor.evt`, while `EndOfUtterance` has been seen under `speech_event`.
  `lib/capture.js` reads both and preserves anything it does not recognise, so an
  unfamiliar payload shows up in the capture rather than vanishing.
