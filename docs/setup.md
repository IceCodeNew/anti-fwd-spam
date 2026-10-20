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

Semantic classification is optional. Review provider privacy and pricing terms before saving a provider key as a Worker secret. No separate OpenAI credential is required for the built-in Luna routes.

The ordered `MODEL_PROVIDERS` mapping in [`src/anti_fwd_spam/constants.py`](../src/anti_fwd_spam/constants.py) owns the supported secret names, endpoints, and model IDs. Configured entries are selected in that order:

1. `EXPERIENTIAL_API_KEY`: GPT-6 Luna Decisions through Experiential.
2. `AI_GATEWAY_API_KEY`: GPT-6 Luna Decisions through Vercel AI Gateway.
3. `TYPESAFE_AI_API_KEY`: legacy Jev through TypeSafe.
4. `OPENCODE_API_KEY`: legacy Jev through OpenCode.
5. `CMD_API_KEY`: legacy Jev through CommandCode.

For example, configure the first route with:

```bash
mise exec -- uv run pywrangler secret put EXPERIENTIAL_API_KEY
```

Only configured entries participate. A legacy-only deployment continues to use Jev. Additional configured entries are error fallbacks within the existing shared retry budget, not additional votes: a completed answer, including a below-threshold, invalid, or refused answer, does not prompt a search for a higher score. The [behavior contract](behavior.md#model-classification) defines decision and retry boundaries. Keep secret values out of files, logs, and command arguments.

The [public provider inventory](model-providers.md) distinguishes built-in routes from documentation-only alternatives and lists their image/video limits. An external platform's model listing does not add a supported Worker secret or enable media ingestion.

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
