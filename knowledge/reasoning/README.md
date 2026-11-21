# Reasoning and channel state

Source: KVV `test_thinking_effort.py` / `test_thinking_extra_body.py`, K31 guide
§§3.2–3.6. Check normalization → effort prompt → reasoning parser → grammar gate.

- Thinking controls are deployment/model contracts. Omitted/default, explicit
  enabled/disabled and effort precedence are different cases.
- K3 observed efforts are low/high/max. K3.1 medium is **unverified** here and
  is not enabled just because a reference implementation accepts it.
- Preserve whitespace-only historical reasoning; exact empty string and whitespace
  are not interchangeable (guide §3.3, reference f45980be67).
- After think closes, require response or tools; allowing immediate termination
  can produce HTTP 200 with an empty public turn.
- Think-close and message-close share a token prefix. A blanket close-token ban
  can deadlock generation. Grammar activation must follow the current channel,
  not any historical close marker, and should be incremental in long contexts.
- Stop matching belongs in response content, not reasoning/tool argument text.

Ordinary positive cases now declare canonical thinking intent, translated by
the selected service dialect (`opensource` versus `kimi`). Raw conflict/alias
probes remain raw and are not rewritten. In particular, a conflict observation
is not proof of precedence. Token-cap/default tests remain explicitly separate.

Do not use model self-report of effort or reasoning-length ordering as a hard
engine oracle. KVV contains noisy/skipped examples of both. Capability cases
check request acceptance, channel shape and nonempty public output; byte/token
renderer parity is the stronger check for exact effort propagation.
