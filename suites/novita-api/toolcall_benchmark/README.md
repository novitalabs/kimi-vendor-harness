# K3 Toolcall Thinking-Effort Benchmark

This is a Novita-owned behavioral reconstruction of Moonshot's private
`kimi_toolcall` task. It is not an upstream source snapshot. The executable
contract was inferred from the official `.eval` artifact and checked against
all 110 scored samples in both the official and Novita comparison artifacts.

The committed gzip fixture contains only the normalized sample inputs, targets,
and tool schemas required to rerun the benchmark. Rebuild it deterministically
from the pinned official artifact with:

```bash
python suites/novita-api/toolcall_benchmark/extract_fixture.py \
  /path/to/official.eval \
  suites/novita-api/fixtures/toolcall-thinking-samples.v1.json.gz
```

The source artifact used `effort=max`; only its effort-independent dataset and
tool schemas are retained. The profile supplies low, high, or max at runtime.

The scorer checks generated tool calls for non-empty arguments, JSON parse
errors, the sample's declared JSON Schema, and placeholder values such as an
empty path or `:` command. Three source samples are known to fail request
tokenization in both supplied artifacts; the profile pins their exact IDs and
requires every other sample to be scored.
