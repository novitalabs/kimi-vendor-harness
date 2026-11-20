# API parameters

The current endpoint case is `protocol.token_limits`. It sends one selected
wire cap and checks reported completion usage; it does not measure actual token
ids. Source inspiration: KVV `kvv_selftest/test_completion_params.py` and K31
guide §3.2. Wire-field exclusivity is separately tested as a harness invariant.

Check API validation → request normalization → renderer kwargs → output cap.
`max_tokens` and `max_completion_tokens` are wire aliases, not two independent
budgets. A test must state which it uses. There are no SDK hidden retries.

KVV's fixed temperature/top_p/penalty rules are **Kimi-platform policy**, not a
universal OpenAI or bare-engine contract. Those sampling-policy probes are not
currently included. A 429/timeout/500 is not evidence that an invalid parameter was correctly
rejected (the old boolean helper conflated these).

Model binding must consistently select renderer, tokenizer regex, tool parser,
reasoning parser and multimodal processor. A model-id substring match in one
component cannot compensate for a wrong renderer in another. Source: guide §3.1.
