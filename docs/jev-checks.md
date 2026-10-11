# Live Jev acceptance checks

The [advertising contract](behavior.md#blacklists-and-content-checks-connect-to-two-actions) defines the expected classification. `tests/jev-cases.json` contains advertising samples and normal controls.

Each case records its source PR, sample type, and reconstruction limits in `source`.

The screenshot samples retain advertising text and context. Names, contact destinations, and campaign identifiers use synthetic placeholders. Unreadable nickname decorations and unavailable biographies remain absent. Invite samples use non-routable `https://t.me/+<invite>` placeholders and partial reference text.

Do not store real advertising destinations in source, tests, or documentation. Use `@<contact>` and `https://example.invalid/` for contact placeholders. Syntax-sensitive parser fixtures use synthetic handles, IDs, and sticker names. Keep the protocol syntax that each fixture tests. Preserve official service endpoints and project links.

Cases with a nonempty `skip` reason retain samples assigned to local regex rules, including the unreferenced group-replacement maintenance pattern. The test reports them as skipped without a provider request. Payment-code samples remain active because PR #30 removed their dedicated regex. Reference cases remain active because references bypass regex. The D1 project-cooperation recruitment case remains active because it is a Jev semantic case, not a keyword regex.

D1 report evidence can preserve a reported message without its nested reply. Completed model tasks discard their input, and the message index stores identifiers only. Use screenshot text for recoverable reply context, label that reconstruction, and leave unavailable profiles absent. Do not infer a historical model score from completed task status.

Add recoverable missed text and normal controls to the same collection. Keep unknown fields absent. Mark synthetic controls and reconstructed context explicitly. Use semantic cases only when the current intent and relation are clear; do not use a fixed phrase, profile-only content, or a fabricated destination as proof of model accuracy. Profile-only personal channel text and quoted discussion remain false-positive controls. Enrichment tests separately verify public-target parsing, source separation, sanitization, unknown fallback, query caps, and one Jev request. They must not treat a profile phrase or a fixed Worker score as proof that Jev's semantic classification is correct. Do not add a skip reason to hide a new model regression. Sticker images and blocked source identities are not text-classification cases.

Export a key from `MODEL_PROVIDERS` in [model.py](../src/anti_fwd_spam/model.py) into your shell. Keep the key outside the repository. Run:

```bash
JEV_LIVE_TEST=1 PYTHONPATH=src uv run python -m unittest tests.test_jev -v
```

The test uses the first configured provider and the production prompt, input formatter, response parser, deadline, and threshold. The fixture currently contains 82 total cases: 5 skipped local-rule cases and 77 active Jev cases. It reports each case separately and sends each active sample once. It does not retry a failed request. A timeout can leave a charged request with an unknown result. For the 2026-10-11 D1 investigation, a three-run matrix over all 77 active cases using `EXPERIENTIAL_API_KEY` found a malicious-score minimum of 0.90 and a non-spam maximum of 0.45 after scoped hints; the inclusive production threshold is therefore 0.90, the lowest allowed value, rather than 0.89. The D1 project-recruitment case scored 0.92, 0.93, and 0.92 in that matrix.

The scoped prompt clarifications have separate evidence boundaries: project recruitment uses current text plus sender biography and raised the D1 case from about 0.61 to 0.92, but it is not applied to ordinary cooperation; the third-party-payment clarification lowered the ordinary service-payment control from about 0.89 to 0.13; the short-service clarification lowered the isolated installation control to about 0.1; and the quoted-channel discussion clarification lowered its false-positive control to about 0.45. Global prompt rewrites were rejected because paired matrices lowered more established positives, including cryptocurrency promotion, SIM offers, and service offers. These are measured trade-offs, not a claim that every provider or future sample will score identically.

These requests send sample content to the provider and can incur charges. The test does not access Telegram or D1. Ordinary unit tests skip this live check. A skip does not prove classification accuracy. Fixed scores in Worker tests verify moderation behavior, not model accuracy.
