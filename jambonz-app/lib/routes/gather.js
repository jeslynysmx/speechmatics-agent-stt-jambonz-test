/*
 * /gather — the production-shaped path.
 *
 * Most voice agents drive recognition through the gather verb rather than
 * transcribe, and gather layers its own end-of-speech handling on top of the
 * engine. That extra layer matters when chasing a segmentation problem: a split
 * seen here but not in /transcribe — or not in the engine-only harness —
 * localizes the fault to that layer.
 *
 * jambonz VAD is left disabled (see lib/recognizer-config.js): end-of-turn comes
 * entirely from Speechmatics' end_of_utterance_silence_trigger.
 *
 * Both hooks are captured:
 *   /gather-partial  partialResultHook — interim results
 *   /gather-final    actionHook        — the utterance jambonz hands the app,
 *                                       i.e. exactly what a downstream
 *                                       extraction pipeline would receive
 */
const {buildRecognizer} = require('../recognizer-config');
const {openCapture, recordSpeechEvent} = require('../capture');

const GATHER_TIMEOUT = parseInt(process.env.GATHER_TIMEOUT || '15', 10);
/* how many utterances to collect before hanging up — segmentation problems
   tend to show up across turns, not within one */
const MAX_TURNS = parseInt(process.env.MAX_TURNS || '20', 10);

const service = ({logger, makeService}) => {
  const svc = makeService({path: '/gather'});

  svc.on('session:new', (session) => {
    const {recognizer, cfg} = buildRecognizer(logger);
    const capture = openCapture({call_sid: session.call_sid, verb: 'gather', cfg});

    session.locals = {
      logger: logger.child({call_sid: session.call_sid}),
      capture,
      cfg,
      recognizer,
      turns: 0,
      counts: {partial: 0, final: 0, eou: 0}
    };
    session.locals.logger.info({recognizer, capture: capture.filePath},
      'new call — gather');

    capture.write({message: 'RecognitionStarted', jambonz_config: recognizer, verb: 'gather'});

    try {
      session
        .on('close', onClose.bind(null, session))
        .on('error', onError.bind(null, session))
        .on('/gather-final', onGatherFinal.bind(null, session))
        .on('/gather-partial', onGatherPartial.bind(null, session));

      session
        .answer()
        .gather(gatherVerb(session))
        .send();
    } catch (err) {
      session.locals.logger.error({err}, 'error responding to incoming call');
      session.close();
    }
  });
};

const gatherVerb = (session) => ({
  input: ['speech'],
  actionHook: '/gather-final',
  partialResultHook: '/gather-partial',
  recognizer: session.locals.recognizer,
  timeout: GATHER_TIMEOUT
});

const onGatherPartial = async(session, evt) => {
  const {logger} = session.locals;
  const {transcript} = recordSpeechEvent(session, evt, {hook: 'partialResultHook'});
  logger.debug(` partial: ${transcript}`);
  session.reply();
};

const onGatherFinal = async(session, evt) => {
  const {logger, capture} = session.locals;

  /* the action hook fires for timeouts too, where there is no speech payload */
  if (evt && evt.speech) {
    const {transcript, confidence} = recordSpeechEvent(session, evt, {hook: 'actionHook'});
    session.locals.turns++;
    logger.info({reason: evt.reason, confidence, turn: session.locals.turns},
      `UTTERANCE HANDED TO APP: ${transcript}`);
    /* this is the unit a downstream pipeline consumes — record the boundary
       explicitly so splits are visible in the rendered capture */
    capture.write({
      message: 'JambonzGatherResult',
      reason: evt.reason,
      turn: session.locals.turns,
      metadata: {transcript}
    }, {hook: 'actionHook'});
  } else {
    logger.info({reason: evt && evt.reason}, 'gather ended with no speech');
    capture.write({message: 'JambonzGatherResult', reason: (evt && evt.reason) || 'unknown'});
  }

  if (session.locals.turns >= MAX_TURNS) {
    /* guard: further hooks can still arrive after the hangup is queued, and
       re-sending it would spam the call */
    if (!session.locals.hungUp) {
      session.locals.hungUp = true;
      logger.info(`reached MAX_TURNS=${MAX_TURNS}, hanging up`);
      return session.hangup().reply();
    }
    return session.reply();
  }

  /* re-arm for the next turn; do NOT speak, so nothing is added to the audio
     under test */
  session.gather(gatherVerb(session)).reply();
};

const onClose = (session, code, reason) => {
  const {logger, capture, counts, turns} = session.locals;
  capture.write({message: 'EndOfTranscript'});
  capture.end();
  logger.info({code, reason, counts, turns},
    `call ended — capture written to ${capture.filePath}`);
};

const onError = (session, err) => {
  const {logger, capture} = session.locals;
  capture.write({message: 'Error', error: err && (err.message || String(err))});
  logger.error({err}, 'session error');
};

module.exports = service;
