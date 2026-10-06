# Live Jev acceptance checks

The [advertising contract](behavior.md#blacklists-and-content-checks-connect-to-two-actions) defines the expected classification. `tests/jev-cases.json` contains advertising samples and normal controls.

Each case records its source PR, sample type, and reconstruction limits in `source`.

The screenshot samples retain advertising text and context. Names, contact destinations, and campaign identifiers use synthetic placeholders. Unreadable nickname decorations and unavailable biographies remain absent. Invite samples use non-routable `https://t.me/+<invite>` placeholders and partial reference text.

Do not store real advertising destinations in source, tests, or documentation. Use `@<contact>` and `https://example.invalid/` for contact placeholders. Syntax-sensitive parser fixtures use synthetic handles, IDs, and sticker names. Keep the protocol syntax that each fixture tests. Preserve official service endpoints and project links.

Cases with a nonempty `skip` reason retain samples assigned to local regex rules. The test reports them as skipped without a provider request. Payment-code samples remain active because PR #30 removed their dedicated regex. Reference cases remain active because references bypass regex.

Add recoverable missed text and normal controls to the same collection. Keep unknown fields absent. Mark synthetic controls and reconstructed context explicitly. Do not add a skip reason to hide a new model regression. Sticker images and blocked source identities are not text-classification cases.

Export a key from `MODEL_PROVIDERS` in [model.py](../src/anti_fwd_spam/model.py) into your shell. Keep the key outside the repository. Run:

```bash
JEV_LIVE_TEST=1 PYTHONPATH=src uv run python -m unittest tests.test_jev -v
```

The test uses the first configured provider and the production prompt, input formatter, response parser, deadline, and threshold. It reports each case separately and sends each active sample once. It does not retry a failed request. A timeout can leave a charged request with an unknown result.

These requests send sample content to the provider and can incur charges. The test does not access Telegram or D1. Ordinary unit tests skip this live check. A skip does not prove classification accuracy. Fixed scores in Worker tests verify moderation behavior, not model accuracy.
