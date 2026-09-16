/*
 * /transcribe — the diagnostic path (compare against /gather).
 *
 * Same Speechmatics config as the /gather route, but driven by
 * the transcribe verb, which streams every interim and final straight through
 * without gather's end-of-speech layer on top. Three-way comparison for the
 * sentence-splitting issue:
 *
 *   engine-only harness  ->  /transcribe  ->  /gather
 *   (no jambonz)             (jambonz media    (jambonz media path +
 *                             path only)        gather's turn logic)
 *
 * Whichever hop introduces the split is the one to fix.
 */
const {buildRecognizer} = require('../recognizer-config');
const {openCapture, recordSpeechEvent} = require('../capture');

/* transcribe is non-blocking, so the call needs parking to keep media flowing */
const PARK_SECONDS = parseInt(process.env.PARK_SECONDS || '3600', 10);

/*
 * REPLY_DELAY_MS deliberately slows our ack to jambonz.
 *
 * jambonz was seen coalescing several Speechmatics messages into one webhook,
 * delaying the earliest by up to 1.8s. Two possible causes: jambonz batches
 * regardless, or it waits for the app's ack and queues whatever arrives in the
 * meantime. Raising this value distinguishes them — if batch sizes grow with the
 * delay, the batching is back-pressure from a slow app (ours sits behind a
 * tunnel; an app on the same LAN would see far less of it). If they do not
 * change, the batching is unconditional.
 */
const REPLY_DELAY_MS = parseInt(process.env.REPLY_DELAY_MS || '0', 10);

const service = ({logger, makeService}) => {
  const svc = makeService({path: '/transcribe'});

  svc.on('session:new', (session) => {
    const {recognizer, cfg} = buildRecognizer(logger);
    const capture = openCapture({call_sid: session.call_sid, verb: 'transcribe', cfg});

    session.locals = {
      logger: logger.child({call_sid: session.call_sid}),
      capture,
      cfg,
      counts: {partial: 0, final: 0, eou: 0}
    };
    session.locals.logger.info({recognizer, capture: capture.filePath},
      'new call — transcribe (diagnostic path)');

    capture.write({message: 'RecognitionStarted', jambonz_config: recognizer, verb: 'transcribe'});

    try {
      session
        .on('close', onClose.bind(null, session))
        .on('error', onError.bind(null, session))
        .on('/transcription', onTranscription.bind(null, session));

      session
        .answer()
        .transcribe({transcriptionHook: '/transcription', recognizer})
        .pause({length: PARK_SECONDS})
        .send();
    } catch (err) {
      session.locals.logger.error({err}, 'error responding to incoming call');
      session.close();
    }
  });
};

const onTranscription = async(session, evt) => {
  const {logger} = session.locals;
  const {isFinal, transcript} = recordSpeechEvent(session, evt, {hook: 'transcriptionHook'});
  if (isFinal) logger.info(`FINAL   : ${transcript}`);
  else logger.debug(` partial: ${transcript}`);

  if (REPLY_DELAY_MS > 0) await new Promise((r) => setTimeout(r, REPLY_DELAY_MS));
  session.reply();
};

const onClose = (session, code, reason) => {
  const {logger, capture, counts} = session.locals;
  capture.write({message: 'EndOfTranscript'});
  capture.end();
  logger.info({code, reason, counts}, `call ended — capture written to ${capture.filePath}`);
};

const onError = (session, err) => {
  const {logger, capture} = session.locals;
  capture.write({message: 'Error', error: err && (err.message || String(err))});
  logger.error({err}, 'session error');
};

module.exports = service;
