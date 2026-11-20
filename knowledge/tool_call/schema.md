# Tool schemas and constrained decoding

## Contract

Schema cases validate the grammar's ability to accept supported schemas and
produce arguments satisfying them. Unsupported or malformed schemas must be
reported as request errors, not crash the service. P/D deployments should check
that both sides use the same schema/error contract.

## Case families

- primitive, object and array arguments
- required/enum/const/additionalProperties
- nested objects and arrays
- `$defs` and `$ref`
- `$ref` with sibling type or description
- numeric/string/array bounds
- invalid schema shape, dangling references and invalid regex

## Development tiers

The smoke set is small. Regression and explicit schema selections are intended
for routine engine work. The full Walle-derived inventory is an extended
feature test, not a benchmark score and not the default developer run.

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
