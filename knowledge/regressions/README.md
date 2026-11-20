# Regression index

Cases are stored once under their primary capability. Select `tag:regression`;
this index links symptoms without creating a duplicate case tree.

| Historical symptom | Capability / likely boundary | Guide evidence |
|---|---|---|
| HTTP 200 empty turn after thinking | reasoning / post-think grammar gate | §3.4, 1b9d46289e |
| tools disable response_format | structured_output/tools_combined / grammar composition | §3.4, PR #340 |
| literal marker truncates arguments | tool_call/arguments / token-aware parser | §3.6, PR #340; some non-strict paths remained open |
| malformed call pollutes whole content | parser / call-local recovery | §3.6, PR #340 |
| compressed history reuses call ids | conversation_state / id allocator | §3.7, a179f4ffea |
| cached_tokens equals all transferred KV | usage / prefill-decode metadata | §3.7, PR #340 |
| invalid schema returns 500 | error_handling / prefill grammar compile | §3.4, PR #340 |
| tool results associated in arrival order | conversation_state / history normalization | §3.3, upstream #50104/#50093/#50420 |
| sparse sampler/grammar state after preemption | grammar / speculative decoding scheduler | §3.4, PR #340 root cause A |

Historical counts and repro rates are not current deployment measurements.
Scheduler/preemption and exact token-id issues cannot be proven by a short black-box
HTTP case alone; keep them as explicit follow-up adapter/runtime coverage gaps.
