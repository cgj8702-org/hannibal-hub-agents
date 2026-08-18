# ADK 429 Rate Limit & Cooldown Details Extraction

This implementation plan details how to intercept, extract, and log rich 429 rate limit, quota, and cooldown details from ADK (`google.adk.models.GoogleLLM` / `_ResourceExhaustedError`) across the Hannibal Hub Agents codebase.

---

## User Review Required

> [!IMPORTANT]
> **No Breaking Changes**: This enhancement is strictly additive and non-breaking. It unmasks the underlying `google.genai.errors.ClientError` nested within ADK's `_ResourceExhaustedError.__cause__` and `response_json`/`response.headers`.

---

## Open Questions

None. The exception structure of ADK 2.0 `_ResourceExhaustedError` and `ClientError` has been verified via scratch validation.

---

## Proposed Changes

### Core Logic & Utilities

#### [MODIFY] [rate_limiter.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/logic/rate_limiter.py)

- Add `extract_rate_limit_details(exc: Exception) -> dict[str, Any]` helper function.
- Inspect `exc.__cause__`, `exc.response_json`, `exc.details`, and `exc.response.headers`.
- Extract:
  - `quota_limit` (e.g. `GenerateContentRequestsPerMinutePerProjectPerRegion`)
  - `quota_value` (e.g. `15`)
  - `retry_after_seconds` (from RPC `retryDelay` / `retry_delay` or HTTP `Retry-After` / `x-ratelimit-reset-requests`)
  - `reason` (e.g. `RATE_LIMIT_EXCEEDED`)
  - Full HTTP header dictionary for clinical telemetry logging.

---

### Webhook Agent Integration

#### [MODIFY] [webhook_agent.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/webhook_agent/webhook_agent.py)

- Intercept `_ResourceExhaustedError` / 429 exceptions during PR code review executions.
- Parse structured rate limit details via `extract_rate_limit_details(exc)`.
- Log clinical warning telemetry containing exact quota name, limit, and cooldown delay.
- Inform fallback model selection or retry backoff queues with exact `retry_after_seconds`.

---

### Feature Agent Integration

#### [MODIFY] [runner.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/src/feature_agent/runner.py)

- Intercept 429 errors during autonomous feature agent task executions.
- Extract `retry_after_seconds` and sleep precisely for the returned cooldown duration before retrying model requests.

---

### Unit Testing

#### [MODIFY] [test_rate_limiter.py](file:///home/carly/coding/synced-repos-cgj8702/hannibal/hannibal-hub-agents/tests/unit/logic/test_rate_limiter.py)

- Add unit test `test_extract_rate_limit_details_from_adk_error()` to verify parsing of:
  - ADK `_ResourceExhaustedError` with nested `ClientError`
  - `QuotaFailure`, `RetryInfo`, and `ErrorInfo` RPC structures
  - HTTP `Retry-After` and `x-ratelimit` headers.

---

## Verification Plan

### Automated Tests
- Execute `./scripts/ruff-all.sh` to ensure 100% linting compliance.
- Execute `uv run python -m pytest tests/unit/logic/test_rate_limiter.py` to verify unit test coverage.
- Execute full test suite `uv run python -m pytest` (155+ tests passing).

### Manual Verification
- Execute scratch verification script to validate extraction against synthetic ADK `_ResourceExhaustedError` instances.
