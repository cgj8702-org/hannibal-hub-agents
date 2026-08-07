# Model Chain Architecture & Dual-Tier Rate Limit Registry

This document details the model fallback chain and comparative rate limit quotas (Free Tier 0 vs Paid Tier 1) enforced by `RPMWaiter` in `hannibal-hub-agents`.

---

## Dynamic Model Fallback Chain

When rate limit errors (`429 RESOURCE_EXHAUSTED`) or transient server errors occur, `WebhookAgent` automatically cascades through the following ordered model chain sorted by Tokens-Per-Minute (TPM) capacity descending:

1. **Configured Primary**: `GEMMA_MODEL` (defaults to `gemini-3.6-flash`)
2. **Tier 1 (4,000,000 TPM / 150k RPD)**: `gemini-3.5-flash-lite`
3. **Tier 2 (2,000,000 TPM / 10k RPD)**: `gemini-3.6-flash`
4. **Tier 3 (1,000,000 TPM / 10k RPD)**: `gemini-2.5-flash`
5. **Tier 4 (16,000 TPM / 14.4k RPD)**: `gemma-4-26b-a4b-it`

---

## Dual-Tier Rate Limit Registry Matrix

The rate limiter dynamically resolves tier via the key resolution cascade (`HANNIBAL_TIER` -> `GEMINI_API_KEY` match against `PAID_KEY` / `FREE_KEY` -> key presence fallback).

| Model | Free Tier RPM | Free Tier TPM | Free Tier RPD | Paid Tier RPM | Paid Tier TPM | Paid Tier RPD |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `models/gemini-3.5-flash-lite` | 15 | 250,000 | 500.0 | 4,000 | 4,000,000 | 150,000.0 |
| `models/gemini-3.6-flash` | 5 | 250,000 | 20.0 | 1,000 | 2,000,000 | 10,000.0 |
| `models/gemini-2.5-flash` | 5 | 250,000 | 20.0 | 1,000 | 1,000,000 | 10,000.0 |
| `models/gemini-3.1-flash-lite` | 15 | 250,000 | 500.0 | 4,000 | 4,000,000 | 150,000.0 |
| `models/gemini-2.5-flash-lite` | 10 | 250,000 | 20.0 | 4,000 | 4,000,000 | 1,000,000.0 |
| `models/gemma-4-31b-it` | 30 | 16,000 | 14,400.0 | 30 | 16,000 | 14,400.0 |
| `models/gemma-4-26b-a4b-it` | 30 | 16,000 | 14,400.0 | 30 | 16,000 | 14,400.0 |
| `models/gemini-2.0-flash` | **0 (Fast-Fail)** | **0** | **0.0** | 2,000 | 4,000,000 | 1,000,000.0 |
| `models/gemini-2.0-flash-lite` | **0 (Fast-Fail)** | **0** | **0.0** | 4,000 | 4,000,000 | 1,000,000.0 |
| `models/gemini-2.5-pro` | **0 (Fast-Fail)** | **0** | **0.0** | 150 | 2,000,000 | 1,000.0 |
| `models/gemini-3.1-pro` | **0 (Fast-Fail)** | **0** | **0.0** | 25 | 2,000,000 | 250.0 |

> **Note on Zero-Quota Models**: Models with `0` RPM/RPD on Free Tier trigger an immediate zero-quota fast-fail (`ValueError`) in `RPMWaiter` to prevent hangs or delayed fallback retries.
