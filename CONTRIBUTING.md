# Contributing and maintenance

This project is a Cloudflare Python Worker. Keep behavior changes small, explicit, and covered at the boundary where users observe them.

## Development setup

```bash
mise trust
mise install
mise exec -- uv sync --locked
mise exec -- npm ci --ignore-scripts
```

Run the full local gate:

```bash
npm test
npm run build -- --outdir .wrangler/test-build
uv run ruff check .
uv run ruff format --check .
uv run --no-default-groups ty check
```

The live model suite is intentionally separate because it sends fixture data to a provider and can incur charges. Follow [live model checks](docs/jev-checks.md) when you have an approved provider setup.

## Source boundaries

- `src/anti_fwd_spam/constants.py` owns static model, input-field, provider, profile-limit, and threshold values, including separate `MODEL_PROVIDERS` (Jev) and `LUNA_PROVIDERS` mappings.
- `src/anti_fwd_spam/rules.py` owns static regexes and backend-specific model prompt text/fragments. Luna clarifications must not rewrite the original Jev prompts or contextual hints.
- `src/anti_fwd_spam/policy.py` owns Telegram update validation, provenance, configuration parsing, and compatibility exports for local matchers.
- `src/anti_fwd_spam/model.py` owns `model_providers(backend)`, `ModelConfig` (whose `backend` defaults to `jev`), backend-specific requests/parsing, model input shaping, profile enrichment, and compatibility exports for established model symbols. Preserve Jev's legacy Vercel protocol as well as its System One routes; a Luna-capable credential is not backend selection.
- `src/anti_fwd_spam/moderation.py` owns routing and action selection.
- `src/anti_fwd_spam/tasks.py` owns durable classification/deletion retries.
- `src/anti_fwd_spam/actions.py` owns Telegram moderation outcomes.

The constants and rules modules must remain dependency-light. When moving a symbol, update its production and test callers to the owning module in the same change. Keep `policy.py`'s matcher functions as the behavior API used by routing and deferred profile checks; do not add compatibility aliases merely to hide an unclear ownership boundary.

## Behavior changes

[AGENTS.md](AGENTS.md) defines the behavior-contract rule. When changing authorization, routing order, moderation targets, blacklist effects, retry behavior, or model classification boundaries:

1. Update `docs/behavior.md` in final-state language.
2. Add or update behavior tests at the owning boundary.
3. Update README links or usage text when user-facing behavior changes.
4. Run the full local gate and inspect `git diff --check`.

Do not turn a provider score, a Worker fake score, or a dated investigation result into a permanent contract without a reproducible fixture and a stated evidence boundary.

## Documentation

The README is the consumer entry point. Deployment commands belong in [docs/setup.md](docs/setup.md), operating procedures in [docs/operations.md](docs/operations.md), stable behavior in [docs/behavior.md](docs/behavior.md), live model test procedure in [docs/jev-checks.md](docs/jev-checks.md), and sourced public-platform/media research in [docs/model-providers.md](docs/model-providers.md). The `jev` fixture, test module, and live-test environment names are compatibility identifiers, not model-selection settings. Keep the English and Chinese README structures aligned, but write idiomatic Chinese rather than translating sentence by sentence.
