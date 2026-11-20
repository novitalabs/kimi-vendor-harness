# Usage and returned metrics

Source: KVV prompt fixtures, Stream Spec revision 244, K31 guide §3.7.
Check token accounting → P/D metadata transfer → serializer, not model quality.

Test integer/nonnegative prompt/completion/total counts, total consistency,
stream/nonstream prompt parity, reasoning_tokens and cached_tokens when the
chosen contract requires them. Missing metrics are not zero. Boolean values are
not integers. Prefix-cache hits must not change the rendered prompt count.

Raw checkpoint tokens, generation-prefix accounting and public API prompt_tokens
are different conventions. Record the convention; do not mask unknown drift with
broad tolerances. K3 historical subtract-three or per-image corrections are not
portable to a different engine/checkpoint. P/D decode must report prefill's true
cache hits rather than treating transferred KV as cache hits. This needs runtime
evidence beyond a single endpoint count.

Response headers/request ids are observability hints. Always preserve their
actual values, not an invented normalised finish_reason or trace id. Framework
adapter concurrency must not leak metadata between requests (KVV contextvar tests).
