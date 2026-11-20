# Tool choice

## Contract

`tool_choice` controls whether the engine may emit tool calls. Keep `none`,
`auto`, and `required` as separate cases; they exercise different request
normalization and grammar paths. Named-function choice is a separate capability
and must not be inferred from `required`.

## Cases

- `tool_call.choice.none`: tools may be declared but no tool call is emitted.
- `tool_call.choice.required`: a declared tool is required and the response must
  finish with `tool_calls`.
- `tool_call.choice.auto`: prompt-dependent; use as a behavioral diagnostic, not
  as a deterministic smoke assertion.

## Likely implementation components

1. OpenAI request normalization and defaulting.
2. Kimi prompt renderer's tool-choice instruction.
3. Grammar selection (`none` must not activate a tool grammar).
4. Response assembly and `finish_reason`.

## Failure hints

- A call under `none`: inspect propagation before inspecting the parser.
- Empty content and no calls under `auto`: inspect the post-thinking response
  channel gate; do not immediately blame the model.
- `required` with no call: inspect grammar activation, then detokenizer/parser.

## Sources

The initial cases are adapted from Moonshot `kvv-k3` K3-Flash feature tests.
They are feature cases, not vendor-score evidence.
