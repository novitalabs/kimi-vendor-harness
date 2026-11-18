# Kimi K3 Flash Test Contract

This profile is the executable local acceptance space for the Kimi K3 Flash vendor contract:

```text
models/kimi-k3-flash/
  profile.yaml
  requirements/rows.yaml
  requirements/source.yaml
  suite-manifest.yaml      # rows, selectors, and Flash-specific values
suites/k3-family.yaml      # shared K3-family test-set definitions
```

The source of truth is Moonshot's K3 Flash vendor document at
<https://moonshot.feishu.cn/wiki/Av7gw9Fg6il1VKkVFMEc84fRn9g>, document revision
122. The interface table is sheet `7ZVUi3` and the accuracy table is sheet
`cPoUBI`, both from spreadsheet `C1uasn5wMhgX4jtSmebcYkjQnxe`, revision 34.
The profile also pins the shared Streaming Output Spec snapshot at Docx
revision 244.

## Executable Coverage

The local profile runs these 11 ordered rows:

| Rows | Coverage | Local evidence |
|---|---|---|
| `1.0`, `1.1` | Streaming Output Spec P0/P1 | The pinned Moonshot self-validation file |
| `2` | Image input | The 25-case fixture, including the 1000-image case |
| `3` | Parameter defaults | Flash KVV `tests/params`, 10 rounds |
| `4` | Tool-call JSON schema | Flash KVV `tests/tool_call_json_schema`, 10 rounds |
| `5` | K3 Flash features | Flash KVV `tests/k3_features`, 10 rounds |
| `6` | Prompt-token behavior | Flash KVV `tests/prompt_tokens`, 10 rounds |
| `7` | Tool-call thinking effort | Shared K3-family KVV `eval.py kimi_toolcall`, max only, pinned `stable2` dataset: 71 samples × 5 epochs = 355 trials |
| `8` | OCRBench | Flash KVV `eval.py ocrbench`, max only |
| `9` | MMMU Pro | Flash KVV `eval.py mmmu`, max only |
| `10` | DeepSWE | Shared DeepSWE runtime, three rounds |

The KVV-derived rows use the pinned official `MoonshotAI/kvv-k3` checkout at
commit `6ec0eaad1ca98933327cf571c0d8b78ab8476551`. This is the shared K3-family
admission library; the current Flash checkout is `kvv-k3`,
K3/K3.1 profile selectors can share the same source pin. The checkout changes
request construction, token-parameter handling, prompt-token fixtures, Walle
schema cases, K3 feature cases, and toolcall benchmark data. The source currently
collects 19 Params cases, 428 Walle cases, 118 K3-feature cases, and 74
prompt-token cases. Its stable2 toolcall benchmark has 71 samples and runs 5
epochs per sample (355 trials). It also adds malformed tool-call rejection and
local `kvv_selftest` contracts. These changes are part of Flash evidence.

Published Flash observations are pinned for tool-call max (`0.979`), OCRBench
max (`0.883`), MMMU Pro max (`0.785`), and DeepSWE.

The Flash KVV checkout uses Git LFS for prompt-token fixtures, toolcall reject
cases, images, and the stable2 toolcall dataset. The checkout must be clean at
the pinned commit and its LFS objects must be materialized before `doctor` or
`accept`; a pointer-only checkout is incomplete evidence.


## DeepSWE Reuse

Row 10 reuses the existing Novita DeepSWE adapter and runtime contract:

- prepared DeepSWE host, local transport;
- the path in `NVST_DEEPSWE_ROOT`;
- DeepSWE commit `435ee89ec2f2e2289f33b0da4f992f0b7b7266b9`;
- Pier `0.3.1`;
- 113 tasks and three rounds;
- one task per Pier job, task-local overlap forbidden;
- error-only recovery after the formal attempts;
- Flash official round scores `0.577`, `0.673`, `0.637`, with `avg@3=0.628`.

The profile accepts a complete average score at or above `0.628`. Simple mode
runs one round for development feedback; full and `precision-core` run three
rounds. This is a profile policy, not a Python model-name branch.


## External Gates

The official document includes requirements that are not represented as local
traffic-generating suites:

- Kimi Code Bench is a closed test owned by the Kimi team. The tested party must
  not save its test data, so it is recorded as an external handoff rather than a
  local `code_bench` suite.
- Moonshot owns the stress test. The external gate covers agreed concurrency and
  TPM, 24-hour duration, TTFT, OTPS, and absence of repetition, crosstalk, or
  nonsensical output.
- Trace ID, token-statistics categories, and `X-Msh-ProviderDeployId` are
  interface/operations requirements recorded in `requirements/source.yaml`.
  They require environment-specific observability evidence and are not silently
  marked passed by this profile.

The profile intentionally does not include K3 Stream P2 or K3 Row 18 tool-call
argument recovery: neither is part of the K3 Flash document's committed local
matrix. K3 and K3 Flash therefore have separate model spaces and separate
requirement hashes even where they share a runner or fixture.

## Commands

Read the profile without vendor traffic:

```bash
uv run nvst list --model kimi-k3-flash
```

Run the complete local Flash acceptance :

```bash
nvst accept --model kimi-k3-flash \
  --service-access service-access.json \
  --judge-access judge-access.json \
  --acceptance full
```

执行正式验收前，先运行零流量契约预检；它会校验 profile、runner、artifact contract，以及 Toolcall 数据集样本数 × epochs 是否等于声明的 355 trials：

```bash
nvst doctor --model kimi-k3-flash \
  --service-access service-access.json \
  --acceptance full
```

如果需要对已部署 endpoint 做显式容量探测，必须额外打开在线 canary 和
capacity probe。该探测在并发 1、10、20 下发送短请求，结果只用于判断服务稳定性，
不计入任何 acceptance 分数：

```bash
nvst doctor --model kimi-k3-flash \
  --service-access service-access.json \
  --acceptance full \
  --online-canary \
  --capacity-probe
```

capacity probe 会记录每一档的成功率、延迟和 failure class。`engine_overloaded`、
429、5xx、timeout 和连接错误属于服务执行稳定性问题，不会被计为模型失败。

Run only the shared DeepSWE selector during development:

```bash
nvst develop --model kimi-k3-flash \
  --service-access service-access.json \
  --select 10
```

The endpoint's served model must be supplied as `kimi-k3-flash` (or the exact
provider deployment name recorded in the secret-free service-access contract).
Credentials remain in the private registry and are never part of this profile.
