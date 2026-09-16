/*
 * Mock jambonz feature-server. Talks the jambonz websocket protocol to the
 * /transcribe route so the capture path can be verified WITHOUT a jambonz
 * account, a phone number or a SIP call.
 *
 * It replays a real engine-only capture (the .raw.jsonl produced by
 * speechmatics_jambonz_test_harness.py), wrapping each Speechmatics message
 * the way jambonz wraps vendor events, i.e. speech.vendor.evt.
 *
 * Usage:
 *   node tools/replay-mock-jambonz.js \
 *     --url ws://localhost:3010/transcribe \
 *     --jsonl ../samples/sample-es-reservation.agent-stt.raw.jsonl
 *
 * For the gather path, point it at /gather instead — the hook names
 * are derived from the url path, so nothing else changes:
 *
 *   node tools/replay-mock-jambonz.js --url ws://localhost:3010/gather --jsonl ...
 *
 * A successful run prints the verbs the app sent (proving the recognizer
 * config is what you think it is) and the path of the capture file written,
 * which should convert to the same transcript as the input.
 */
const WebSocket = require('ws');
const {readFileSync} = require('fs');

const argOf = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 ? process.argv[i + 1] : dflt;
};

const url = argOf('url', 'ws://localhost:3010/transcribe');
const jsonl = argOf('jsonl', '../samples/sample-es-reservation.agent-stt.raw.jsonl');
const paced = process.argv.includes('--paced');
/*
 * --evt-shape array  emits the post-fix jambonz shape: speech.vendor.evt is an
 * array whose elements nest the raw Speechmatics object at [i].vendor.evt.
 * Use it to verify the app handles both shapes before spending a cluster call.
 */
const evtShape = argOf('evt-shape', 'single');
const wrapEvt = (raws) => (evtShape === 'array'
  ? raws.map((r) => ({vendor: {name: 'speechmatics', evt: r}}))
  : raws[0]);
/* /transcribe has a single hook; /gather splits partials and finals in two */
const isGather = url.includes('/gather');
const hookFor = (isFinal) => {
  if (!isGather) return '/transcription';
  return isFinal ? '/gather-final' : '/gather-partial';
};
const callSid = argOf('call-sid', 'mock-call-sid-0001');

const entries = readFileSync(jsonl, 'utf8')
  .split('\n')
  .filter((l) => l.trim())
  .map((l) => JSON.parse(l));

const transcriptOf = (m) => {
  const meta = (m.metadata && m.metadata.transcript) || '';
  if (meta.trim()) return meta.trim();
  return (m.results || []).reduce((acc, r) => {
    const content = ((r.alternatives || [])[0] || {}).content || '';
    if (!content) return acc;
    return acc + (acc && r.attaches_to !== 'previous' ? ' ' : '') + content;
  }, '');
};

const ws = new WebSocket(url);
let msgid = 0;
const nextMsgid = () => `mock-${++msgid}`;

const send = (obj) => ws.send(JSON.stringify(obj));

ws.on('open', () => {
  console.log(`connected to ${url}`);
  send({
    type: 'session:new',
    msgid: nextMsgid(),
    call_sid: callSid,
    data: {
      call_sid: callSid,
      direction: 'inbound',
      from: '+15550001111',
      to: '+15550002222',
      call_status: 'trying',
      sip_status: 100,
      account_sid: 'mock-account-sid'
    }
  });
});

let replayStarted = false;
let verbsShown = false;
let pendingChunks = [];

ws.on('message', async(data) => {
  const msg = JSON.parse(data);
  if (msg.data && Array.isArray(msg.data)) {
    if (!verbsShown) {
      verbsShown = true;
      console.log('\n--- verbs received from app ---');
      console.log(JSON.stringify(msg.data, null, 2));
      console.log('--- end verbs ---\n');
    } else {
      const names = msg.data.map((v) => v.verb).join(', ');
      console.log(`app re-armed: ${names}`);
    }
  }
  if (replayStarted) return;
  replayStarted = true;

  const t0 = Date.parse(entries[0] && entries[0].received_at) || 0;

  for (const entry of entries) {
    const m = entry.message || {};
    if (!['AddPartialTranscript', 'AddTranscript', 'EndOfUtterance'].includes(m.message)) continue;

    if (paced && t0) {
      const due = Date.parse(entry.received_at) - t0;
      await new Promise((r) => setTimeout(r, Math.max(0, due - (Date.now() - startedAt))));
    }

    const isFinal = m.message !== 'AddPartialTranscript';
    const hook = hookFor(isFinal);

    /*
     * In gather mode, buffer chunks and emit one final per chunk that carries an
     * is_eos mark — approximating one actionHook per utterance rather than one
     * per AddTranscript.
     *
     * CAUTION: this rule is an assumption about jambonz, not observed behaviour.
     * A chunk containing two is_eos marks yields ONE utterance here, which is why
     * the reservation clip replays as 6 utterances for 7 engine sentences. Real
     * jambonz may split them. Do not cite mock output as evidence about jambonz.
     */
    if (isGather && isFinal) {
      pendingChunks.push(m);
      const endsUtterance = (m.results || []).some((r) => r.is_eos);
      if (!endsUtterance) continue;
      const text = pendingChunks.map(transcriptOf).filter(Boolean).join(' ');
      const merged = {
        ...m,
        results: pendingChunks.flatMap((c) => c.results || []),
        metadata: {...(m.metadata || {}), transcript: text}
      };
      pendingChunks = [];
      send({
        type: 'verb:hook',
        msgid: nextMsgid(),
        call_sid: callSid,
        hook,
        data: {
          reason: 'speechDetected',
          speech: {
            is_final: true,
            language_code: 'es',
            alternatives: [{transcript: text, confidence: 0.9}],
            vendor: {name: 'speechmatics', evt: wrapEvt([merged])}
          }
        }
      });
      if (!paced) await new Promise((r) => setTimeout(r, 2));
      continue;
    }

    send({
      type: 'verb:hook',
      msgid: nextMsgid(),
      call_sid: callSid,
      hook,
      data: {
        speech: {
          is_final: isFinal,
          language_code: 'es',
          alternatives: [{transcript: transcriptOf(m), confidence: 0.9}],
          vendor: {name: 'speechmatics', evt: wrapEvt([m])}
        }
      }
    });
    if (!paced) await new Promise((r) => setTimeout(r, 2));
  }

  console.log(`replayed ${entries.length} entries; closing session`);
  await new Promise((r) => setTimeout(r, 250));
  ws.close();
});

const startedAt = Date.now();

ws.on('close', () => {
  console.log('session closed — check ./captures for the written .raw.jsonl');
  process.exit(0);
});
ws.on('error', (err) => {
  console.error('websocket error:', err.message);
  process.exit(1);
});
