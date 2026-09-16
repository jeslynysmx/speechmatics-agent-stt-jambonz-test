module.exports = ({logger, makeService}) => {
  /* the production-shaped path (gather) and the diagnostic path (transcribe) */
  require('./gather')({logger, makeService});
  require('./transcribe')({logger, makeService});
};
