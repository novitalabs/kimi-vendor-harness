# Tool declaration and dynamic tools

## Contract

Kimi feature tests may declare tools at request level and, for Kimi-specific
new-model behavior, inside system messages. Dynamic tools must retain their
message position and must be merged with global tools without silently changing
names or order.

## Implemented case families

- global function declaration
- duplicate names within one declaration
- global plus dynamic declaration
- dynamic tools in first, middle and last system messages
- nested parameter schema
- invalid tool name/type/shape
- dynamic message role/content validation

The 128-character boundary, mixed supported/unsupported types, duplicates across
distinct dynamic messages and the full upstream matrix are not covered here.
Role/content/name validation is a pinned Kimi-platform contract, not a universal
OpenAI rule; these negative probes require `--allow-opt-in`.

## Likely implementation components

- OpenAI message schema and request normalization
- dynamic-tool extraction
- Kimi prompt renderer (`New Tools Available` section)
- tool registry and grammar schema conversion
- response tool-name validation

## Source

Adapted from `MoonshotAI/kvv-k3` `tests/k3_features/test_dynamic_tools.py`
at the revisions recorded in `sources/provenance.yaml`. The KVV runtime is not
required to run the capability catalog.
