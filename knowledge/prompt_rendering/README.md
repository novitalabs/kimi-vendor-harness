# Prompt rendering and tokenizer parity

Source: checkpoint encoder first; K31 guide §§2, 3.1, 3.3, 4. KVV prompt fixtures
are useful examples, not a universal token oracle for every model/frontend.

Check tokenizer selection → ordinary-text versus structural ids → segmented
renderer → generation prefix. Never tokenize a concatenated XTML string as a
substitute for segmented encoding: user/tool text can literally spell markers,
and segment boundaries affect tokenization.

Tool declarations preserve array order and recursively sort JSON keys. Implicit
server defaults (notably strict=true) must not silently enter the prompt. Source
f45980be67 records a historical +6-token tool-declaration discrepancy. History
must preserve whitespace reasoning and non-object/invalid raw arguments according
to the target encoder, not a convenient reference implementation.

Parallel tool results map to the most recent assistant call set by id and original
position. **Token totals cannot detect a missing re-sort** because reordered
segments may have identical lengths. Use an encoder token-vector adapter or
separate semantic continuation probe; label the latter model-behavior dependent.

Token fixtures record original expected counts without automatic vLLM +3/+image
corrections. Exact-oracle cases require an explicitly verified tokenizer revision
and accounting convention; parity cases compare equivalent endpoint requests.
K3.1 special ids and JSON literal rules remain unverified until its checkpoint
is pinned. Optional renderer adapters compare full token-id sequences offline.
