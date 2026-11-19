# Credentials

Tokens never go into this repository, command arguments, run artifacts, or chat.
Access files (`service-access.json`, `judge-access.json`) only *name* an
environment variable.

## Endpoint token

`scripts/kvh` looks for the token in this order:

1. `KIMI_API_KEY` environment variable.
2. The private registry `~/.config/novita-kimi-vendor/credentials.json`
   (override with `NVST_CREDENTIAL_REGISTRY`), matched by endpoint URL.

Register an endpoint once (token is read from stdin, not argv):

```bash
read -r -s AUTH   # paste: Authorization: Bearer <token>
printf '%s' "$AUTH" | "$NVST_HARNESS_PYTHON" scripts/nvst_credentials.py set \
  --name my-endpoint --role endpoint --exact-host my-endpoint.example.com
unset AUTH

"$NVST_HARNESS_PYTHON" scripts/nvst_credentials.py list    # redacted fingerprints only
"$NVST_HARNESS_PYTHON" scripts/nvst_credentials.py remove --name my-endpoint
```

Matching: exact host beats longest host suffix (`--host-suffix`), then longest
`--path-prefix`; equal matches are rejected as ambiguous. The registry
directory is mode `0700`, files `0600`; do not sync it anywhere.

## BEAM judge token (Row 16 only)

BEAM uses a separate judge endpoint described by `cases/judge-access.example.json`.
Copy it to `cases/judge-access.json` (ignored by Git), export the variable it
names (default `PPIO_JUDGE_API_KEY`), and pass `--judge-access cases/judge-access.json`.

## If a token leaks

Revoke it at the provider first, issue a new one, then `set` the same registry
name again. Deleting the local entry alone does not make a leaked token safe.
