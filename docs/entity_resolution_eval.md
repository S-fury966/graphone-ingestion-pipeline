# Entity Resolution — Synthetic Evaluation Corpus

> **Synthetic test corpus — NOT live data.** Labelled cases for the deterministic entity resolver (`src/resolver/`). Full per-case results are in [`entity_resolution_eval.csv`](entity_resolution_eval.csv).

## Accuracy summary

- **Precision: 1.000** (false merges = 0 / 37 negatives)
- **Recall: 1.000** (96 / 96 positive merges correct)
- **False merges: 0** (negatives that were wrongly merged into an existing entity)
- Total cases: 133 (96 positive, 37 negative)

Definitions match `tests/test_resolver.py`: recall over positive cases, precision = 1 − wrong_merges / negatives.

**All cases pass.**

