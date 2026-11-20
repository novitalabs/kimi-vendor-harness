# Conversation state

Source: K31 guide §§3.3, 3.6, 3.7 and historical Row 18 recovery fixtures.
Check history normalization → prompt renderer → id allocation → parser.

- Associate tool results using tool_call_id against the most recent assistant
  tool-call turn; tool result name may be stale and completion order may differ.
- Compressed history can contain sparse numeric id suffixes. Continue from the
  maximum suffix, not the number of surviving history calls, when the selected
  Kimi id contract requires this. Generic OpenAI deployments may use other ids.
- Preserve reasoning/content/tool-call channels and malformed historical
  arguments as required by the checkpoint encoder.
- Historical Row 18 has two long requests (143/19 replayed messages). They remain
  opt-in expensive regressions with one pass, not hidden six-attempt acceptance.

A semantic reorder probe is not a token-level oracle: a model can answer by
reading content even if associations were rendered incorrectly. Strong ordering
proof needs an encoder adapter comparing token ids. Concurrent request/finish
metadata tests belong in harness unit tests, not a model-quality score.
