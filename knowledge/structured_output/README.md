# Structured output and tools

Case families: `structured_output/json_object`, `structured_output/json_schema`,
`structured_output/tools_combined`. Sources: KVV `test_response_format.py`, K31
porting guide §§3.4, 5. Implement response grammar and tool grammar as compatible
branches; do not let tool grammar silently overwrite response_format.

## Known failure modes

- JSON works without tools but not with tools: inspect grammar composition.
- Thinking + json_object produces an empty key: the grammar may block the Kimi
  response-channel header. Enter response, then constrain JSON.
- strict schema accepts invalid output: inspect constrained decoding, not just
  the prompt instruction. Use an adversarial prompt as well as a cooperative one.

`tool_choice=auto` may legitimately choose tools; a JSON-output assertion only
applies to the text branch. `none` forces the text branch for deterministic
composition checks. JSON syntax, object type and schema validity are separate
checks. Empty-key JSON is legal unless a specific regression/schema forbids it.

Validate strict JSON with Draft 2020-12, including additionalProperties and
booleans versus numbers. Non-strict schema is not evidence of full schema
conformance. No score threshold or model-quality benchmark is involved.
