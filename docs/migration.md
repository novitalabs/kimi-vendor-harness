# Capability-harness migration

This repository intentionally moved from a K3 vendor-acceptance harness to a
Kimi agentic capability/regression harness.

## Retained

- Novita-owned request/response and stream regressions, where they are small,
  reproducible and explain an engine boundary.
- K3/K3-Flash feature cases adapted from KVV: tool choice, dynamic tools,
  response formats, thinking fields, selected schema cases and prompt/stream
  contracts.
- Kimi XTML parser vectors under `capabilities/prompt_rendering/`.
- K31 tool-call guide knowledge, decomposed by capability.

## Adapted

KVV cases are not copied as a second official suite. Each retained case records:

- source repository/path/test and observed commit;
- the exact adaptation;
- capability, likely implementation components and request budget;
- determinism and model-specific assumptions.

A selected KVV case is therefore a development contract, not proof of official
coverage or vendor acceptance.

## Excluded

The following are deliberately removed from the active repository surface:

- BEAM, OCRBench, MMMU Pro and DeepSWE execution;
- large tool-call quality benchmark runners and score thresholds;
- official ten-repeat orchestration, formal acceptance modes and acceptance
  sessions;
- remote runtime packs, judge access and benchmark-specific artifact schemas;
- the full vendored KVV checkout as a runtime dependency.

The historical KVV source was inspected at
`MoonshotAI/kvv-k3@9ad4c1871574d7117a0297fcac9365722253d15c`; the harness does
not require that checkout to run. The pinned parser vectors retain their own
vLLM-int provenance. Future KVV refreshes should re-extract feature cases and
update provenance rather than reintroducing a whole vendor tree.

## Why

Benchmarks can expose a useful defect. When that happens, minimize the defect
into a capability or regression case and link the benchmark incident in
`knowledge/regressions/`. Do not add the benchmark runner or turn its score into
a capability verdict.
