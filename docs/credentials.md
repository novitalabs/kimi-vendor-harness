# Credentials

Access contracts contain endpoint identity and the name of an environment
variable. They never contain Authorization, cookies, bearer tokens or signed
URLs. Copy `cases/service-access.example.json` to an ignored local file and set
its `api_key_env` to the variable you own.

```bash
export KIMI_API_KEY='...'
```

For local matching without putting a token in a command argument, use the
private registry helper:

```bash
read -r -s AUTH
printf '%s' "$AUTH" | "$NVST_HARNESS_PYTHON" scripts/nvst_credentials.py set \
  --name kimi-endpoint --role endpoint --host-suffix example.com
unset AUTH
"$NVST_HARNESS_PYTHON" scripts/nvst_credentials.py list
```

The capability runner redacts the configured secret and credential-looking
strings before writing run evidence. It does not persist Authorization headers.
There is no judge credential: benchmark judge workflows are outside this
repository.
