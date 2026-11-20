# Tool schemas and constrained decoding

## Contract

Schema cases validate the grammar's ability to accept supported schemas and
produce arguments satisfying them. Unsupported or malformed schemas must be
reported as request errors, not crash the service. P/D deployments should check
that both sides use the same schema/error contract.

## Implemented cases

- A required object schema with string and number arguments.
- Local `$defs`/`$ref` with a sibling type or chained description reference.
- A curated nested object/array schema with required fields, enums, booleans,
  additionalProperties and integer/array bounds.
- Invalid type, dangling reference and malformed required-field shapes.

These are a selected subset. Full dialect support, recursive references, regex
coverage, const and a complete primitive/bounds matrix are not established.

## Development tiers

The smoke set is small. `tool_call/schema/references` selects the added local-ref
and nested-schema cases; invalid-schema probes live under `error_handling`.
The full Walle-derived inventory is **not included**. No selected subset should
be presented as a Walle pass or a model-quality benchmark.

## Likely implementation components

- schema normalization and dialect support
- grammar compilation and token-id mapping
- grammar activation after thinking
- response/tool grammar composition
- request-error serialization

## Sources and cautions

KVV's validator wraps some schemas and strips `default`. An extracted case
must disclose such adaptation and retain the original schema/provenance.
Do not claim complete JSON Schema support from a selected subset.
