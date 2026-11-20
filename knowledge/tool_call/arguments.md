# Tool arguments and parser boundaries

## Contract

Tool arguments must remain valid JSON and retain their meaning through
non-streaming and streaming assembly. Typed arguments and raw JSON blocks are
different parser paths. Literal marker text is data; real structural token ids
are protocol boundaries.

## Existing vectors

- typed string/number/boolean/object/array arguments
- raw JSON byte preservation
- marker text inside a JSON string
- malformed call header isolates only that call
- undeclared tool is dropped
- truncated marker is not flushed into content
- attribute unescaping

These vectors are framework-adapter tests, not endpoint tests. A model endpoint
cannot be forced to emit the exact token sequence of a parser regression.

## Likely implementation components

- tokenizer/detokenizer special-token handling
- incremental XTML parser state machine
- JSON string/escape tracking
- call-local error isolation
- tool-call delta assembly

## Known traps

A text-only parser cannot always distinguish a true `<|close|>` token id from
identical ordinary-character text. Do not infer the root cause from rendered
text alone. Preserve the raw response and, where available, token ids.

## Source

K3 parser vectors from `novitalabs/vllm-int:k3-flash-kvv@c94c52e24e40`, plus
K31 tool-call porting guidance. Reference implementation choices are not the
model contract.
