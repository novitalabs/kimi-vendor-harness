# Errors and service validity

Source: KVV negative feature cases, Stream Spec revision 244, K31 guide §§3.4, 3.7.
Malformed request/schema rejection must be the expected client status and error
shape. A timeout, auth error or 500 must never satisfy 'request was rejected'.

Check validation → grammar compilation → frontend error serializer. In P/D,
compile can fail on prefill too; both sides need the same version/contract.
Request-level failures before streaming must be JSON errors, not fake SSE success.

Transport/auth/quota/rate-limit/server failures are execution/service failures,
not scored model failures. However a test specifically asserting 'invalid schema
must not crash' must retain a 500 as evidence of that failed contract rather
than auto-retry it away. No implicit retries. Request_id/trace_id must survive
error paths without leaking another request's context or any credential.
