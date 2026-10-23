# Live model checks

This document defines the repeatable procedure for testing GPT-6 Luna Decisions semantic classification. It is a test runbook, not a record of any single investigation or provider result.

## Fixture rules

- Keep semantic cases in `tests/decision-cases.json` with an expected `spam` label and a `source` note.
- Use sanitized text, synthetic usernames, placeholder contacts such as `@<contact>`, and reserved URLs such as `https://example.invalid/`.
- Keep real member IDs, phone numbers, report JSON, credentials, and private destinations out of the repository.
- Mark reconstructed context and unavailable fields explicitly; do not infer historical scores from deleted or completed task state.
- Keep semantic positives, semantic negatives, and reference cases active. A local-rule match is not grounds to skip an established-sender semantic case: established humans reach the model even for the same text. Derive each model label from visible advertising or attack intent, independently of the stricter low-history regex label, and record any historical label correction in the source note.
- Add paired positive and negative controls for each boundary: current-sender intent, quoted promotion versus warning/analysis, profile-only context, product/service discussion, and ordinary technical or personal experience.

## Running the check

The acceptance harness in `tests/test_decision.py` uses the production formatter, static message and joining-profile prompts, provider-specific response parser, timeout, and threshold. It selects the first configured provider from `MODEL_PROVIDERS` in `src/anti_fwd_spam/constants.py` and sends each active case once. See [provider setup](setup.md#optional-model-provider) for keys and selection order. This single-call semantic check is not a live test of durable provider failover or retry scheduling.

With an approved Luna provider credential configured, opt into the live test:

```bash
DECISION_LIVE_TEST=1 PYTHONPATH=src uv run python -m unittest tests.test_decision -v
```

The provider credential must be available in the local test process environment; deployed Worker secrets are not automatically available locally. Do not put the credential in a repository file or command argument. The test can incur provider charges and sends fixture text to the provider; run it only with an approved credential and data set. Experiential's free allowance is finite, not a guarantee that a run is free.

## Interpreting results

- A skipped local-rule case is not evidence about model accuracy.
- A Worker fake score proves routing and moderation lifecycle behavior, not semantic model quality.
- Treat provider errors, timeouts, invalid envelopes, refusals, and valid model scores as separate result categories.
- Report the fixture revision, selected provider/model, active/skipped case counts derived from the fixture, score range, and false-positive/false-negative cases.
- Verify native Decisions predicate and OpenRouter Noul parsing with deterministic tests; malformed, refused, mismatched, extra native, or non-finite answers must never produce punishment or score-shopping. Worker fakes cannot prove a public gateway accepts the wire format.
- For changes to the shared prompts, compare baseline and candidate prompts against the same active fixtures and provider/model. Repeat boundary cases when scores are near the threshold; do not declare a prompt safe from one run.
- Record text and image transport evidence separately. Successful Experiential text responses do not verify the other Luna platforms or image inputs; the bot does not submit actual media.
- Keep historical score matrices and investigation-specific rationale in the pull request or an external experiment record, not in this durable runbook.
