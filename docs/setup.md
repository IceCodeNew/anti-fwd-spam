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

Semantic classification is optional and uses GPT-6 Luna Decisions. Review provider privacy and pricing terms before saving a provider key as a Worker secret. A supported key enables classification.

`MODEL_PROVIDERS` in [`src/anti_fwd_spam/constants.py`](../src/anti_fwd_spam/constants.py) owns the ordered keys, endpoints, and model IDs. Missing or empty keys are skipped; without a configured supported key, model classification is unavailable.

| Priority | Platform | Worker secret |
| --- | --- | --- |
| 1 | Experiential | `EXPERIENTIAL_API_KEY` |
| 2 | OpenAI (official) | `OPENAI_API_KEY` |
| 3 | OpenRouter | `OPENROUTER_API_KEY` |
| 4 | Vercel AI Gateway | `AI_GATEWAY_API_KEY` |

For example, enable Luna through Experiential:

```bash
mise exec -- uv run pywrangler secret put EXPERIENTIAL_API_KEY
```

Add only the other provider secrets intended for failover. `OPENAI_API_KEY` is required only for the official OpenAI route; the gateway routes use their own credentials. Experiential, official OpenAI, and Vercel use native Decisions requests; OpenRouter uses its System One-style Noul interface. One static message prompt and one static joining-profile prompt apply across providers, without conditional hints.

### Compatibility and migration

**Breaking change:** Jev is retired. Existing deployments with `EXPERIENTIAL_API_KEY` or `AI_GATEWAY_API_KEY` now use Luna, including its different endpoint and request format. Review provider access, terms, and billing before deploying.

`TYPESAFE_AI_API_KEY`, `OPENCODE_API_KEY`, and `CMD_API_KEY` are ignored. A deployment with only those keys has no model classification; configure at least one supported key from the table above to enable Luna. Local rules, reports, and blacklists still operate without a model.

### Failover and costs

Additional configured providers are connection/HTTP error fallbacks, not extra votes or score-shopping. A completed answer ends classification, including a below-threshold, invalid, or refused answer. Classification has a fixed shared budget of four total attempts, not four per provider; provider rotation does not reset it. Existing durable retry delays remain owned by `RETRY_DELAYS` in [`src/anti_fwd_spam/tasks.py`](../src/anti_fwd_spam/tasks.py). See the [behavior contract](behavior.md#model-classification) for error and decision boundaries.

Experiential offers a free allowance, not unlimited free usage. Exhausted allowance can produce an error and move work to a configured paid provider; Experiential usage itself may also be billable under the account's terms. This is an ordered provider waterfall, not a strict-free mode or a cost cap. Configure provider-side billing limits and omit paid credentials when paid failover is not intended. Keep secret values out of files, logs, and command arguments.

The bot submits text only; provider image capability does not enable media ingestion.

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
