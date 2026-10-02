# Continuous Quality Evaluation Suite (`tests/eval/`)

This directory houses the Agent Platform / ADK Quality Flywheel continuous evaluation datasets, metric configurations, and benchmark baselines for the Webhook Agent code auditor.

## Architecture

- **`eval_config.yaml`**: Evaluation specification defining evaluation thresholds and LLM-as-judge scoring metrics.
- **`datasets/`**: Curated golden datasets representing real and synthetic pull request diffs.
  - `pr_reviews.json`: Ground truth diff samples paired with expected verdicts (`APPROVE`, `REQUEST_CHANGES`) and expected risk counts.

## Evaluation Metrics

| Metric | Target Threshold | Description |
|---|---|---|
| `zero_hallucinated_risks` | 1.0 (100%) | Measures whether clean PRs return risks as an empty list `[]`. Prevents nit-picking or false-positive alarms on clean code. |
| `diff_grounding_accuracy` | 0.95 (95%) | Verifies that line numbers cited in identified risks correspond to genuine modified diff lines. |
| `prompt_purity` | 1.0 (100%) | Guarantees that meta-prompts, developer directives, or ADK internal orchestrator strings do not leak into GitHub comments or review markdown. |

## Execution

The evaluation suite runs during model quality audits and pre-release benchmarking against Gemini models (`gemini-3.5-flash-lite`, `gemma-4-31b-it`) using the Agent Platform evaluation runner:

```bash
uv run python -m pytest tests/eval/
```
