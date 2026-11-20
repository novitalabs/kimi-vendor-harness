# API parameters

Case families: `protocol/token_limits`, `protocol/validation`. Source: KVV
`tests/params/test_params.py`, `kvv_selftest/test_completion_params.py`, K31 guide §3.2.

Check API validation → request normalization → renderer kwargs → output cap.
`max_tokens` and `max_completion_tokens` are wire aliases, not two independent
budgets. A test must state which it uses. There are no SDK hidden retries.

KVV's fixed temperature/top_p/penalty rules are **Kimi-platform policy**, not a
universal OpenAI or bare-engine contract. They are explicit opt-in platform
cases. A 429/timeout/500 is not evidence that an invalid parameter was correctly
rejected (the old boolean helper conflated these).

Model binding must consistently select renderer, tokenizer regex, tool parser,
reasoning parser and multimodal processor. A model-id substring match in one
component cannot compensate for a wrong renderer in another. Source: guide §3.1.
