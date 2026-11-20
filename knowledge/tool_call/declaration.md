# Tool declaration and dynamic tools

## Contract

Kimi feature tests may declare tools at request level and, for Kimi-specific
new-model behavior, inside system messages. Dynamic tools must retain their
message position and must be merged with global tools without silently changing
names or order.

## Case families

- global function declaration
- duplicate names within one declaration
- global plus dynamic declaration
- dynamic tools in first, middle and last system messages
- nested parameter schema
- invalid tool name/type/shape
- dynamic message role/content validation
- 128-character tool name boundary

## Likely implementation components

- OpenAI message schema and request normalization
- dynamic-tool extraction
- Kimi prompt renderer (`New Tools Available` section)
- tool registry and grammar schema conversion
- response tool-name validation

## Source

Adapted from `MoonshotAI/kvv-k3` K3-Flash `tests/k3_features/test_dynamic_tools.py`
and `tests/params/test_params.py`. The KVV runtime is not required to run the
capability catalog.
