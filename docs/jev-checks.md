# Live Jev checks

This document defines the repeatable procedure for testing semantic Jev classification. It is a test runbook, not a record of any single investigation or provider result.

## Fixture rules

- Keep semantic cases in `tests/jev-cases.json` with an expected `spam` label and a `source` note.
- Use sanitized text, synthetic usernames, placeholder contacts such as `@<contact>`, and reserved URLs such as `https://example.invalid/`.
- Keep real member IDs, phone numbers, report JSON, credentials, and private destinations out of the repository.
- Mark reconstructed context and unavailable fields explicitly; do not infer historical scores from deleted or completed task state.
- Skip only cases owned by deterministic local rules. Keep semantic positives, semantic negatives, and reference cases active.
- Add paired positive and negative controls for each boundary: current-sender intent, quoted promotion versus warning/analysis, profile-only context, product/service discussion, and ordinary technical or personal experience.

## Running the check

The acceptance harness in `tests/test_jev.py` uses the production formatter, prompt assembly, response parser, timeout, and threshold. It selects the first configured provider from `MODEL_PROVIDERS` and sends each active case once.

```bash
JEV_LIVE_TEST=1 PYTHONPATH=src uv run python -m unittest tests.test_jev -v
```

The provider credential must be available through the deployment/runtime environment used for the test. Do not put the credential in a repository file or command argument. The test can incur provider charges and sends fixture text to the provider; run it only with an approved credential and data set.

## Interpreting results

- A skipped local-rule case is not evidence about Jev accuracy.
- A Worker fake score proves routing and moderation lifecycle behavior, not semantic model quality.
- Treat provider errors, timeouts, invalid envelopes, and model scores as separate result categories.
- Report the fixture revision, provider/model selected by `MODEL_PROVIDERS`, active/skipped case counts derived from the fixture, score range, and false-positive/false-negative cases.
- For prompt changes, compare baseline and candidate prompts against the same active fixtures and provider. Repeat boundary cases when scores are near the threshold; do not declare a prompt safe from one run.
- Keep historical score matrices and investigation-specific rationale in the pull request or an external experiment record, not in this durable runbook.
