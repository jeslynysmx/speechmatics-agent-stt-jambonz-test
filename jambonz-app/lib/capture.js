/*
 * Writes Speechmatics events to a .raw.jsonl in the SAME schema as
 * ../speechmatics_jambonz_test_harness.py, so the engine-only run and the
 * through-jambonz run are rendered by the same --convert and diffed directly.
 *
 * Extra keys (t_offset_ms, verb, jambonz_is_final) are additive; the converter
 * only reads received_at and message.
 */
const {createWriteStream, mkdirSync} = require('fs');
const path = require('path');

const CAPTURE_DIR = process.env.CAPTURE_DIR || path.join(process.cwd(), 'captures');

const openCapture = ({call_sid, verb, cfg}) => {
  mkdirSync(CAPTURE_DIR, {recursive: true});
  const name = `${call_sid}.jambonz-${verb}.max_delay_${cfg.maxDelay}` +
    `.eou_${cfg.eouSilence}.raw.jsonl`;
  const filePath = path.join(CAPTURE_DIR, name);
  const stream = createWriteStream(filePath, {flags: 'a'});
  const t0 = Date.now();

  const write = (message, extra = {}) => {
    stream.write(JSON.stringify({
      received_at: new Date().toISOString(),
      t_offset_ms: Date.now() - t0,
      verb,
      ...extra,
      message
    }) + '\n');
  };

  return {filePath, write, end: () => stream.end(), t0};
};

/*
 * Normalise speech.vendor.evt to a flat list of raw Speechmatics events.
 *
 * As of the Aug 2026 adapter fix, jambonz sends two shapes:
 *   - exactly one AddTranscript -> the raw Speechmatics object itself
 *   - more than one             -> an array whose elements wrap the raw object
 *                                  one level deeper, at [i].vendor.evt
 *
 * Flattening keeps the capture 1:1 with what the engine emitted, so the
 * converter and the analysis tools see the same structure either way.
 */
const rawEventsOf = (speech) => {
  const evt = speech && speech.vendor && speech.vendor.evt;
  if (!evt) return [];
  const list = Array.isArray(evt) ? evt : [evt];
  return list
    .map((e) => {
      if (!e) return null;
      if (e.message) return e;                                  /* raw already */
      if (e.vendor && e.vendor.evt) return e.vendor.evt;        /* nested form */
      return null;
    })
    .filter((e) => e && e.message);
};

/*
 * jambonz surfaces EndOfUtterance on the transcription hook under its own
 * `speech_event` key, NOT inside speech.vendor.evt. Reading only the latter made
 * the Aug 2026 captures record each EOU as an empty AddTranscript and report
 * "zero EOU events" — a harness artifact, not a jambonz defect. Read both.
 */
const speechEventName = (evt, speech) => {
  const raw = (evt && (evt.speech_event || evt.event)) || (speech && speech.speech_event);
  if (!raw) return null;
  if (typeof raw === 'string') return raw;
  return raw.type || raw.name || raw.event || JSON.stringify(raw);
};

const recordSpeechEvent = (session, evt, {hook} = {}) => {
  const {logger, capture, counts} = session.locals;
  const speech = (evt && evt.speech) || evt || {};
  const isFinal = speech.is_final !== false;
  const alt = (speech.alternatives || [])[0] || {};

  const named = speechEventName(evt, speech);
  if (named) {
    const isEou = /end.?of.?(utterance|turn)/i.test(named);
    capture.write({
      message: isEou ? 'EndOfUtterance' : named,
      _jambonz_speech_event: named,
      _jambonz_payload: evt
    }, {hook});
    if (isEou) counts.eou++;
    logger.info(`speech_event: ${named}${isEou ? '  (counted as EndOfUtterance)' : ''}`);
    return {isFinal, transcript: alt.transcript || '', speechEvent: named};
  }

  const raws = rawEventsOf(speech);
  const shape = Array.isArray(speech.vendor && speech.vendor.evt) ?
    `array[${speech.vendor.evt.length}]` : 'single';

  if (raws.length) {
    /* log the shape once so a future change to it is visible in the capture */
    if (!session.locals.seenShapes) session.locals.seenShapes = new Set();
    if (!session.locals.seenShapes.has(shape)) {
      session.locals.seenShapes.add(shape);
      logger.info(`speech.vendor.evt shape seen: ${shape}`);
    }

    for (const raw of raws) {
      capture.write(raw, {jambonz_is_final: isFinal, hook, evt_shape: shape});
      const key = {
        AddPartialTranscript: 'partial',
        AddTranscript: 'final',
        EndOfUtterance: 'eou'
      }[raw.message];
      if (key) counts[key]++;
    }
  } else {
    capture.write({
      message: isFinal ? 'AddTranscript' : 'AddPartialTranscript',
      metadata: {transcript: alt.transcript || ''},
      results: [],
      _synthesized_from_jambonz_payload: true,
      /* keep the whole payload — an unrecognised shape must be visible in the
         capture rather than flattened into an empty final */
      _jambonz_payload: evt
    }, {jambonz_is_final: isFinal, hook});
    counts[isFinal ? 'final' : 'partial']++;
    if (!session.locals.warnedNoRaw) {
      session.locals.warnedNoRaw = true;
      logger.warn('no usable speech.vendor.evt — capture lacks word timings and ' +
        'is_eos, so sentence-split analysis will be limited');
    }
  }

  return {isFinal, transcript: alt.transcript || '', confidence: alt.confidence};
};

module.exports = {openCapture, recordSpeechEvent, CAPTURE_DIR};
