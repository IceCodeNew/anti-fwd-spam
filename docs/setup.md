# Deployment setup

This guide deploys the bot to Cloudflare Workers, creates its D1 database, saves Telegram secrets, and connects Telegram's webhook. Use a disposable test group first.

## Prerequisites

Install Git and [`mise`](https://mise.jdx.dev/getting-started.html), create a [Cloudflare account](https://dash.cloudflare.com/sign-up), and use Bash. Windows users can use WSL.

```bash
git clone https://github.com/IceCodeNew/anti-fwd-spam.git
cd anti-fwd-spam
mise trust
mise install
mise exec -- uv sync --locked
mise exec -- npm ci --ignore-scripts
mise exec -- uv run pywrangler login
```

The login command opens a browser. Sign in to the Cloudflare account that will host the Worker. Check current Workers and D1 pricing/limits in Cloudflare's documentation; model providers have separate pricing and terms.

## Telegram bot and permissions

1. Send `/newbot` to [@BotFather](https://t.me/BotFather), then save the token and username.
2. Disable Group Privacy Mode. If the bot must receive messages from other bots, enable [Bot-to-Bot Communication Mode](https://core.telegram.org/api/bots/bot-to-bot).
3. Add the bot as an administrator in a test supergroup with permission to delete messages and restrict members.
4. For channel comments, use the channel's linked discussion group.

## D1 database

Create the database and copy the returned ID into `wrangler.jsonc` under `d1_databases[].database_id`. Keep the binding as `REPORTS` and keep the scheduled trigger.

```bash
mise exec -- uv run pywrangler d1 create anti-fwd-spam-reports
mise exec -- uv run pywrangler d1 migrations apply anti-fwd-spam-reports --remote
```

## Worker secrets

Keep the bot token in the current shell only until webhook registration is complete. The reporting allowlist accepts user IDs and sender-chat IDs separated by commas; an empty allowlist disables direct report moderation, source registration, and `/allow` recovery.

First obtain trusted user IDs by privately messaging the new bot, then query updates:

```bash
read -rsp 'Bot token: ' BOT_TOKEN; printf '\n'
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates"
```

Do not publish this output: it contains member information. Save the following as Worker secrets:

```bash
mise exec -- uv run pywrangler secret put BOT_USERNAME
mise exec -- uv run pywrangler secret put REPORTER_IDS
printf '%s' "$BOT_TOKEN" | mise exec -- uv run pywrangler secret put BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET="$(mise exec -- python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s' "$TELEGRAM_WEBHOOK_SECRET" | mise exec -- uv run pywrangler secret put TELEGRAM_WEBHOOK_SECRET
```

## Optional model provider

Semantic classification is optional. Review provider privacy and pricing terms before saving a provider key as a Worker secret. Set `MODEL_BACKEND` explicitly to opt into Luna; saving a Luna-capable provider key alone does not select it.

| `MODEL_BACKEND` | Selection |
| --- | --- |
| Unset, empty, or `jev` | Jev, with its original prompts, provider order, and request protocols, including Vercel's legacy evaluation-model protocol. |
| `luna` | GPT-6 Luna Decisions, using a separate provider chain and Luna-specific prompts. |
| Any other value | Configuration error; not an automatic fallback to either backend. |

[`model_providers(backend)` in `src/anti_fwd_spam/model.py`](../src/anti_fwd_spam/model.py) selects the backend's ordered mapping. `MODEL_PROVIDERS` and `LUNA_PROVIDERS` in [`src/anti_fwd_spam/constants.py`](../src/anti_fwd_spam/constants.py) own the keys, endpoints, and model IDs. Missing or empty keys are skipped; no configured key for the selected backend means no model classification.

### Jev providers

Jev uses `MODEL_PROVIDERS` in this order:

| Priority | Platform | Worker secret |
| --- | --- | --- |
| 1 | TypeSafe | `TYPESAFE_AI_API_KEY` |
| 2 | Experiential | `EXPERIENTIAL_API_KEY` |
| 3 | OpenCode | `OPENCODE_API_KEY` |
| 4 | CommandCode | `CMD_API_KEY` |
| 5 | Vercel AI Gateway | `AI_GATEWAY_API_KEY` |

For example, leave `MODEL_BACKEND` unset and configure an existing Jev route:

```bash
mise exec -- uv run pywrangler secret put EXPERIENTIAL_API_KEY
```

### Luna opt-in and providers

Luna uses `LUNA_PROVIDERS` in this order, independently of the Jev chain:

| Priority | Platform | Worker secret |
| --- | --- | --- |
| 1 | Experiential | `EXPERIENTIAL_API_KEY` |
| 2 | OpenAI (official) | `OPENAI_API_KEY` |
| 3 | OpenRouter | `OPENROUTER_API_KEY` |
| 4 | Vercel AI Gateway | `AI_GATEWAY_API_KEY` |

To opt into Luna through Experiential:

```bash
printf '%s' luna | mise exec -- uv run pywrangler secret put MODEL_BACKEND
mise exec -- uv run pywrangler secret put EXPERIENTIAL_API_KEY
```

Add only the other provider secrets intended for failover. `OPENAI_API_KEY` is required only for the official OpenAI route; the gateway routes use their own credentials. Experiential and Vercel keys are shared setting names across backends, but their endpoint, model, prompt, and wire format follow `MODEL_BACKEND`. Luna does not use Jev-only credentials or fall back to Jev.

### Failover and costs

Within the selected backend, additional configured providers are connection/HTTP error fallbacks, not extra votes or score-shopping. A completed answer ends classification, including a below-threshold, invalid, or refused answer. Both backends retain the fixed shared budget of four total attempts, not four per provider; provider rotation does not reset it. Existing durable retry delays remain owned by `RETRY_DELAYS` in [`src/anti_fwd_spam/tasks.py`](../src/anti_fwd_spam/tasks.py). See the [behavior contract](behavior.md#model-classification) for error and decision boundaries.

Experiential offers a free allowance, not unlimited free usage. Exhausted allowance can produce an error and move work to a configured paid provider; Experiential usage itself may also be billable under the account's terms. This is an ordered provider waterfall, not a strict-free mode or a cost cap. Configure provider-side billing limits and omit paid credentials when paid failover is not intended. Keep secret values out of files, logs, and command arguments.

The [public provider inventory](model-providers.md) distinguishes the four built-in Luna platforms from research-only alternatives and lists documented image/video limits. Provider image capability does not enable media ingestion in this bot.

## Deploy and register the webhook

```bash
mise exec -- uv run pywrangler deploy
read -rp 'Worker HTTPS URL: ' WORKER_URL
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WORKER_URL%/}/webhook" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","edited_message"]'
unset TELEGRAM_WEBHOOK_SECRET
```

The webhook response must contain `"ok":true` and `"result":true`. One bot token can have only one active webhook.

## First verification

Use a disposable group and test account. Apply all D1 migrations before running new Worker code. Verify an authorized report, a source message, an ordinary message, a join-profile decision and `/allow` recovery, and an administrator-protection case. Remove test blacklist entries and Telegram restrictions when finished. For troubleshooting and false-positive recovery, see [Operations](operations.md).
