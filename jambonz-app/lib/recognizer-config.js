/*
 * The recognizer config, in one place.
 *
 * The defaults below are a worked example of a production voice-agent setup —
 * Spanish, enhanced, aggressive finalization, punctuation pinned — so an
 * unconfigured run reproduces a realistic pipeline rather than a bare one.
 * Change them to match what you are actually testing:
 *
 *   language es, operating_point enhanced
 *   max_delay 0.7, max_delay_mode flexible      <- the production baseline that
 *                                                  causes the sentence splitting
 *   punctuation_overrides . , ? ! @ sensitivity 0.5
 *   conversation_config.end_of_utterance_silence_trigger 0.7  (was 0.5)
 *   enable_entities true                        <- smart formatting es/pt
 *   additional_vocab from config/sample-vocab.json
 *   jambonz VAD disabled                        <- end-of-turn comes from the
 *                                                  recognizer, not from jambonz
 *
 * Two ways to override, so a max_delay sweep needs neither a code edit nor an
 * app restart:
 *   - env vars (read at startup)
 *   - config/recognizer-overrides.json (re-read on every call)
 */
const {readFileSync, existsSync} = require('fs');
const path = require('path');

const CONFIG_DIR = process.env.CONFIG_DIR || path.join(__dirname, '..', 'config');
const VOCAB_FILE = process.env.VOCAB_FILE || path.join(CONFIG_DIR, 'sample-vocab.json');
const OVERRIDES_FILE = process.env.OVERRIDES_FILE ||
  path.join(CONFIG_DIR, 'recognizer-overrides.json');

const num = (v, dflt) => (v === undefined || v === '' ? dflt : parseFloat(v));
const bool = (v, dflt) => (v === undefined || v === '' ? dflt : v === 'true');

const envDefaults = () => ({
  /* If the speech credential in the jambonz portal has a Label, it must be named
     here or jambonz looks for an unlabelled Speechmatics credential and fails to
     find one. Leave unset only if the credential's Label field is blank. */
  label: process.env.SM_CREDENTIAL_LABEL || null,
  language: process.env.SM_LANGUAGE || 'es',
  operatingPoint: process.env.SM_OPERATING_POINT || 'enhanced',
  maxDelay: num(process.env.SM_MAX_DELAY, 0.7),
  maxDelayMode: process.env.SM_MAX_DELAY_MODE || 'flexible',
  eouSilence: num(process.env.SM_EOU_SILENCE, 0.7),
  enableEntities: bool(process.env.SM_ENABLE_ENTITIES, true),
  enablePartials: bool(process.env.SM_ENABLE_PARTIALS, true),
  punctuationSensitivity: num(process.env.SM_PUNCT_SENSITIVITY, 0.5),
  permittedMarks: (process.env.SM_PERMITTED_MARKS || '.,?!').split('').filter((c) => c.trim()),
  /* With jambonz VAD off, end-of-turn comes from Speechmatics alone. Be
     deliberate about this: it puts the whole segmentation load on the
     recognizer's EOU trigger and on max_delay. */
  jambonzVad: bool(process.env.JAMBONZ_VAD, false),
  /*
   * Speechmatics Agent STT (model linden-1, wss://preview.rt.speechmatics.com/v2/agent).
   *
   * jambonz exposes it as a SEPARATE VENDOR, chosen in the portal's Speech tab,
   * not as a `model` field — sending model inside transcription_config is
   * rejected by verb-specifications ("unknown property model"). So selecting it
   * means naming that vendor and the credential's Label.
   *
   * It is a different API, not a mode of the RT one: no max_delay,
   * max_delay_mode, enable_entities or conversation_config, and it emits
   * AddSegment/EndOfTurn rather than AddTranscript/EndOfUtterance. agentMode
   * therefore sends only the fields it documents — sending an RT field is how
   * you end up debugging a rejected StartRecognition.
   *
   * Measured against the API directly on 16 Sep 2026: punctuation_overrides is
   * NOT honoured either, despite the spec sheet listing it — the service
   * answers "Field transcription_config.punctuation_overrides is not valid
   * config for this session and was ignored".
   */
  agentMode: bool(process.env.SM_AGENT_MODE, false),
  vendor: process.env.SM_VENDOR || null,
  diarization: process.env.SM_DIARIZATION || null,
  profile: process.env.SM_PROFILE || null,
  host: process.env.SM_HOST || null,
  /* jambonz's continuous-ASR timer. jambonz say the transcribe task runs in
     continuous-ASR mode and holds finals until the turn closes on this timer or
     on EndOfUtterance; we never set it, so null tests their default and a value
     tests whether the hold tracks the timer */
  asrTimeout: process.env.SM_ASR_TIMEOUT === undefined || process.env.SM_ASR_TIMEOUT === '' ?
    null : num(process.env.SM_ASR_TIMEOUT, null),
  /* transcript_filtering_config needs jambonz v11+; older instances reject the
     field outright, so leave it off unless you know the server takes it */
  transcriptFiltering: bool(process.env.SM_TRANSCRIPT_FILTERING, false),
  removeDisfluencies: bool(process.env.SM_REMOVE_DISFLUENCIES, false),
  useVocab: bool(process.env.SM_ADDITIONAL_VOCAB, true)
});

const readJson = (file) => {
  if (!existsSync(file)) return null;
  try {
    return JSON.parse(readFileSync(file, 'utf8'));
  } catch (err) {
    return {_error: `${file}: ${err.message}`};
  }
};

/*
 * jambonz "hints" do NOT reach Speechmatics, so vocabulary bias only happens if
 * it is sent explicitly as additional_vocab. Accepts either a flat list of
 * strings or Speechmatics' {content, sounds_like} objects.
 */
const loadVocab = () => {
  const raw = readJson(VOCAB_FILE);
  if (!raw) return [];
  const entries = Array.isArray(raw) ? raw : (raw.additional_vocab || []);
  return entries
    .map((e) => (typeof e === 'string' ? {content: e} : e))
    .filter((e) => e && e.content);
};

/*
 * Agent STT. Deliberately minimal: only what the API documents, so a surprise
 * in the capture is the service's behaviour and not our config.
 */
const buildAgentRecognizer = (cfg) => {
  const transcription_config = {
    language: cfg.language,
    enable_partials: cfg.enablePartials
  };
  if (cfg.diarization) transcription_config.diarization = cfg.diarization;

  const recognizer = {
    vendor: cfg.vendor || 'speechmatics agent',
    language: cfg.language,
    interim: cfg.enablePartials,
    punctuation: true,
    speechmaticsOptions: {transcription_config}
  };
  if (cfg.host) recognizer.speechmaticsOptions.host = cfg.host;
  if (cfg.label) recognizer.label = cfg.label;
  if (!cfg.jambonzVad) recognizer.vad = {enable: false};
  return {recognizer, cfg};
};

const buildRecognizer = (logger) => {
  const cfg = envDefaults();

  /* file overrides win over env, and are re-read per call so a sweep can just
     rewrite the file between calls */
  const overrides = readJson(OVERRIDES_FILE);
  if (overrides && overrides._error) {
    if (logger) logger.warn(`ignoring recognizer overrides — ${overrides._error}`);
  } else if (overrides) {
    Object.assign(cfg, overrides);
  }

  if (cfg.agentMode) return buildAgentRecognizer(cfg);

  const transcription_config = {
    language: cfg.language,
    operating_point: cfg.operatingPoint,
    enable_partials: cfg.enablePartials,
    max_delay: cfg.maxDelay,
    max_delay_mode: cfg.maxDelayMode,
    punctuation_overrides: {
      permitted_marks: cfg.permittedMarks,
      sensitivity: cfg.punctuationSensitivity
    },
    conversation_config: {
      end_of_utterance_silence_trigger: cfg.eouSilence
    }
  };

  if (cfg.enableEntities) transcription_config.enable_entities = true;

  if (cfg.useVocab) {
    const vocab = loadVocab();
    if (vocab.length) transcription_config.additional_vocab = vocab;
  }

  if (cfg.transcriptFiltering) {
    /* jambonz v11+ only; older instances reject this */
    transcription_config.transcript_filtering_config = {
      remove_disfluencies: cfg.removeDisfluencies,
      ...(cfg.replacements && {replacements: cfg.replacements})
    };
  }

  const speechmaticsOptions = {transcription_config};
  if (cfg.profile) speechmaticsOptions.profile = cfg.profile;
  if (cfg.host) speechmaticsOptions.host = cfg.host;

  const recognizer = {
    vendor: 'speechmatics',
    language: cfg.language,
    interim: cfg.enablePartials,
    punctuation: true,
    speechmaticsOptions
  };

  if (cfg.asrTimeout !== null && cfg.asrTimeout !== undefined) {
    recognizer.asrTimeout = cfg.asrTimeout;
  }
  if (cfg.label) recognizer.label = cfg.label;
  if (!cfg.jambonzVad) recognizer.vad = {enable: false};

  return {recognizer, cfg};
};

module.exports = {buildRecognizer, envDefaults, loadVocab, VOCAB_FILE, OVERRIDES_FILE};
