# Versioned vLLM implementation search map

Reference only: `novitalabs/vllm-int@9e2bdb7111bdffce9e38f085a9f96649aaed3daa`
(`k3_production`) and K3-Flash PR #340 (`e712466dc2`). Derived from the K31
porting guide dated 2026-11-16. Paths/choices are not asserted current upstream
or correct K3.1 behavior.

| Generic boundary | Reference files |
|---|---|
| request validation | rust/src/server/src/routes/openai/chat_completions/validate.rs |
| request conversion / tool ids | rust/src/server/src/routes/openai/chat_completions/convert.rs |
| renderer selection | rust/src/chat/src/renderer/selection.rs |
| Kimi encoding | rust/src/chat/src/renderer/kimi_k3/encoding.rs; vllm/renderers/kimi_k3.py |
| tool grammar | rust/src/parser/src/unified/kimi_k3/structural_tag.rs; vllm/tool_parsers/structural_tag_registry.py |
| grammar composition | rust/src/chat/src/output/default/structural_tag.rs |
| parser | rust/src/parser/src/unified/kimi_k3.rs; vllm/tool_parsers/kimi_k3_tool_parser.py |
| reasoning/grammar gate | vllm/reasoning/kimi_k3_reasoning_parser.py; vllm/v1/structured_output/__init__.py |
| grammar token masking | vllm/v1/structured_output/backend_xgrammar.py |
| response / usage | rust/src/server/src/routes/openai/chat_completions.rs |
| thinking budget | vllm/v1/worker/gpu/sample/thinking_budget.py |

Known warnings in that guide: with_reasoning(false), exact model-name usage
trigger, inconsistent renderer/parser detection, multiple incompatible id formats,
and unverified K3.1 medium/literal-JSON behavior. Never copy these as a contract.
