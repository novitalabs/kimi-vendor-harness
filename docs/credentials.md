# Credentials

Access contracts contain endpoint identity and the name of an environment
variable. They never contain Authorization, cookies, bearer tokens or signed
URLs. Copy `cases/service-access.example.json` to an ignored local file and set
its `api_key_env` to the variable you own.

```bash
export KIMI_API_KEY='...'
```

The runner reads only the named environment variable, not the private registry.
If you use the optional registry helper, store a **complete Authorization header**
via stdin, then resolve it into the environment without printing it:

```bash
read -r -s AUTH
printf '%s' "$AUTH" | "$NVST_HARNESS_PYTHON" scripts/nvst_credentials.py set \
  --name kimi-endpoint --role endpoint --host-suffix example.com
unset AUTH
"$NVST_HARNESS_PYTHON" scripts/nvst_credentials.py list
# list shows public metadata; resolve emits a secret, so capture it, never log it.
AUTH=$("$NVST_HARNESS_PYTHON" scripts/nvst_credentials.py resolve \
  --name kimi-endpoint --role endpoint)
export KIMI_API_KEY="${AUTH#Authorization: Bearer }"
unset AUTH
```

The capability runner redacts the configured secret and credential-looking
strings before writing run evidence. It does not persist Authorization headers.
Legacy registry judge entries are not used by this runner: benchmark judge
workflows are outside this repository. Never enable shell tracing (`set -x`)
while loading credentials. Redaction is defense-in-depth, not a substitute for
keeping credentials out of requests and fixtures.
