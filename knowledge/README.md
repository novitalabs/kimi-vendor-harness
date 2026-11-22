# Kimi capability knowledge

Start at the failing case's `knowledge` links. Cases are organized by capability,
not official row or test source. One case can have multiple tags and component
hints without being duplicated.

## Evidence hierarchy

1. Target checkpoint encoder, tokenizer and config for rendering/token identities.
2. Versioned Kimi API/protocol contract for externally observable behavior.
3. Pinned reference implementation for examples, never as the authority.
4. Historical incident/handoff as a hypothesis until independently verified.

The K31 porting guide has been decomposed into the pages below. Historical rates,
open defects and upstream PR titles are not current service evidence. K3.1's
encoder, medium effort, literal JSON preservation and token ids require separate
verification; K3 values must not silently become K3.1 ground truth.

- [API parameters](protocol/README.md)
- [Tool choice](tool_call/choice.md), [declaration](tool_call/declaration.md),
  [arguments/parser](tool_call/arguments.md), [schema](tool_call/schema.md)
- [Structured output](structured_output/README.md)
- [Reasoning](reasoning/README.md)
- [Streaming](streaming/README.md)
- [Prompt rendering](prompt_rendering/README.md)
- [Usage](usage/README.md)
- [Conversation state](conversation_state/README.md)
- [Multimodal](multimodal/README.md)
- [Errors](error_handling/README.md)
- [Regressions](regressions/README.md)

For new regressions, follow the
[case-authoring workflow](../docs/capability-taxonomy.md#add-a-regression).
Component hints are search starting points, not proof. For a pinned vLLM
implementation, see the [source map](frameworks/vllm-int.md).
