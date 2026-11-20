# Streaming

Source: pinned Moonshot Stream Spec revision 244 and K31 guide §§3.6–3.7.
Keep raw frames: accumulating first can conceal missing role, missing finish,
missing usage, duplicate finish and wrong call index.

Check SSE decoder → incremental parser → tool deltas → response serializer.
Track choice index separately from tool index. First tool delta carries id/type/
name and empty arguments; later deltas concatenate arguments at the same index.
A finish frame and [DONE] are distinct. Missing finish or DONE is incomplete,
not a default `stop`. Usage may be top-level or choice-level per the selected
contract. Different choices must not be merged into choice zero.

Parser vectors exercise arbitrary chunk boundaries and malformed calls, but
cannot prove real endpoint traffic. Text vectors cannot establish token-id
identity; literal-marker versus special-id behavior requires a token-aware
adapter. Stream lifetime after DONE is also distinct from content correctness.
