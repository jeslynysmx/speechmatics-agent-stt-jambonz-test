#!/usr/bin/env bash
#
# Place one call into jambonz with a wav file as the caller's audio, so the
# Speechmatics recognizer sees it exactly as it would see a live caller.
#
#   1. converts the wav to an RTP pcap (G.711, 20ms frames)
#   2. REGISTERs a SIP user to your jambonz account's sip realm
#   3. dials DEST from the same local port and replays the pcap as media
#   4. hangs up once the audio has played plus a finalization tail
#
# The app's /transcribe route writes the Speechmatics events to ./captures/.
#
# Required env:
#   SIP_REALM   your jambonz account's sip realm, e.g. acme.sip.jambonz.cloud
#   SIP_USER    a SIP user created in the jambonz portal
#   SIP_PASS    that user's password
#
# Optional env:
#   DEST        digits to dial (default 1000) — with "Application for SIP device
#               calls" set on the account, any value routes to your app
#   AUDIO       wav/mp3 to inject (default ../samples/sample-es-reservation.wav)
#   CODEC       pcmu (default) or pcma
#   TAIL_MS     extra hold time after playback, for finalization (default 6000)
#   LOCAL_PORT  local SIP port, kept the same for REGISTER and INVITE (default 5070)
#   SBC_HOST    where to send SIP (default: $SIP_REALM)
#
set -euo pipefail
cd "$(dirname "$0")/.."

# Shell environments do not survive opening a new terminal, so keep the SIP
# settings in a file. Anything already exported wins over the file.
if [[ -f .env.injector ]]; then
  # shellcheck disable=SC1091
  set -a; source .env.injector; set +a
  echo "==> loaded settings from .env.injector"
fi

: "${SIP_REALM:?set SIP_REALM in .env.injector (see .env.injector.example)}"
: "${SIP_USER:?set SIP_USER}"
: "${SIP_PASS:?set SIP_PASS}"

DEST="${DEST:-1000}"
AUDIO="${AUDIO:-../samples/sample-es-reservation.wav}"
CODEC="${CODEC:-pcmu}"
TAIL_MS="${TAIL_MS:-6000}"
LOCAL_PORT="${LOCAL_PORT:-5070}"
SBC_HOST="${SBC_HOST:-$SIP_REALM}"
WORK="captures"

command -v sipp >/dev/null || {
  echo "sipp not found — install with: sudo apt-get install -y sip-tester" >&2; exit 1; }

# ngrok's free URL changes on every restart, and the jambonz application keeps
# pointing at the dead host — the call then goes nowhere with no obvious error.
# Surface the live URL every run so a mismatch is caught before the call.
TUNNEL=$(curl -sS --max-time 3 http://127.0.0.1:4040/api/tunnels 2>/dev/null \
  | python3 -c "
import json,sys
try:
    for t in json.load(sys.stdin).get('tunnels', []):
        if t.get('proto') == 'https':
            print(t['public_url']); break
except Exception:
    pass
" 2>/dev/null)
if [[ -n "$TUNNEL" ]]; then
  echo "==> live tunnel: $TUNNEL"
  echo "    the jambonz application's webhooks MUST be wss://${TUNNEL#https://}/<route>"
  echo "    if they name a different host, update them before continuing"
else
  echo "==> WARNING: no ngrok tunnel found on 127.0.0.1:4040 — is ngrok running?" >&2
fi

mkdir -p "$WORK"
PCAP="$WORK/$(basename "$AUDIO").$CODEC.pcap"
python3 tools/wav-to-rtp-pcap.py --audio "$AUDIO" --out "$PCAP" --codec "$CODEC"

# hold the call for the audio duration plus a tail so trailing finals arrive
DURATION_MS=$(python3 -c "
import struct,sys
d=open('$PCAP','rb').read()
off,n=24,0
while off<len(d):
    _,_,cl,_=struct.unpack('<IIII',d[off:off+16]); off+=16+cl; n+=1
print(int(n*20))
")
HOLD_MS=$((DURATION_MS + TAIL_MS))

USERS="$WORK/sip-user.csv"
printf 'SEQUENTIAL\n%s\n' "$SIP_USER" > "$USERS"

echo "==> registering $SIP_USER@$SIP_REALM via $SBC_HOST"
sipp "$SBC_HOST" \
  -sf tools/sipp/register.xml \
  -p "$LOCAL_PORT" -m 1 -inf "$USERS" \
  -key realm "$SIP_REALM" \
  -au "$SIP_USER" -ap "$SIP_PASS" \
  -timeout 20 -trace_err -error_file "$WORK/register-errors.log"

echo "==> dialing $DEST, injecting $AUDIO ($((HOLD_MS / 1000))s hold)"
sipp "$SBC_HOST" \
  -sf tools/sipp/inject-audio.xml \
  -p "$LOCAL_PORT" -m 1 -s "$DEST" -inf "$USERS" \
  -key realm "$SIP_REALM" -key pcap "$PCAP" \
  -au "$SIP_USER" -ap "$SIP_PASS" \
  -d "$HOLD_MS" -timeout $(( HOLD_MS / 1000 + 30 )) \
  -trace_err -error_file "$WORK/inject-errors.log"

echo
# Distinguish "jambonz never reached us" from "it reached us and something failed".
# A stale ngrok host makes jambonz report X-Reason: 404 Not Found, which looks
# like an app bug but is really a dead tunnel.
if [[ -n "$TUNNEL" ]]; then
  HITS=$(curl -sS --max-time 3 http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | python3 -c "
import json,sys
try:
    ts = json.load(sys.stdin).get('tunnels', [])
    print(sum(t.get('metrics', {}).get('http', {}).get('count', 0) for t in ts))
except Exception:
    print('?')
" 2>/dev/null)
  echo "==> requests seen by the tunnel: ${HITS:-?}"
  if [[ "$HITS" == "0" ]]; then
    echo "    jambonz never reached this tunnel. The application's webhook almost"
    echo "    certainly names a stale ngrok host — a dead host returns HTTP 404,"
    echo "    which jambonz reports on the call as 'X-Reason: 404 Not Found'."
    echo "    Set both webhooks to wss://${TUNNEL#https://}/<route> and retry."
  fi
fi

echo
echo "==> done. Speechmatics events captured under $WORK/:"
ls -t "$WORK"/*.jambonz-*.raw.jsonl 2>/dev/null | head -3 || echo "  (none — is the app running and reachable via your tunnel?)"
echo
echo "Render the newest capture with:"
echo "  python3 ../speechmatics_jambonz_test_harness.py --convert \\"
echo "    \$(ls -t $WORK/*.jambonz-*.raw.jsonl | head -1) --no-partials"
