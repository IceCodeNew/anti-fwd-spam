# anti-fwd-spam

English | [简体中文](README_zh-Hans.md)

A Telegram moderation bot hosted on Cloudflare Workers, with blocklists and report records stored in Cloudflare D1. Use it to report spam, block messages from specified source bots, and optionally check new messages with the Jev model.

Start in a test group. Bans can delete message history, and unbanning cannot restore deleted messages.

See [Message processing and reporting](docs/behavior.md) for flow diagrams, blacklist routing, and the reporting-plugin contract.

## 1. Create a Telegram bot

1. Send `/newbot` to [@BotFather](https://t.me/BotFather) and follow the prompts. Save the bot token and username.
2. In BotFather, disable Group Privacy Mode. To receive messages from other bots, also enable [Bot-to-Bot Communication Mode](https://core.telegram.org/api/bots/bot-to-bot).
3. Add the bot to your group as an administrator with permission to delete messages and ban users. For a channel's comments, use its linked discussion group.

Use a supergroup, such as a channel's linked discussion group, for all moderation features. Telegram does not support permanent mutes in basic groups.

## 2. Prepare the deployment tools

Create a [Cloudflare account](https://dash.cloudflare.com/sign-up) and install [Git](https://git-scm.com/downloads) and [mise](https://mise.jdx.dev/getting-started.html). On Windows, use a Linux terminal through WSL. Run the commands below in Bash, in the same terminal session.

```bash
git clone https://github.com/IceCodeNew/anti-fwd-spam.git
cd anti-fwd-spam
mise trust
mise install
mise exec -- uv sync --locked
mise exec -- npm ci --ignore-scripts
mise exec -- uv run pywrangler login
```

The login command opens a browser. Sign in to the Cloudflare account that will host the bot. If any command fails, resolve the error before continuing.

Cloudflare offers free allowances for [Workers](https://developers.cloudflare.com/workers/platform/pricing/) and [D1](https://developers.cloudflare.com/d1/platform/pricing/). Check the limits and monitor usage in the dashboard; model providers charge separately.

## 3. Create the database

```bash
mise exec -- uv run pywrangler d1 create anti-fwd-spam-reports
```

Open `wrangler.jsonc` in a text editor. Replace the `database_id` under `d1_databases` with the ID returned by this command. Keep `binding` as `REPORTS` and keep the other settings, including the scheduled trigger in `triggers`.

Create the database tables:

```bash
mise exec -- uv run pywrangler d1 migrations apply anti-fwd-spam-reports --remote
```

## 4. Configure the bot and reporting identities

Enter the bot token from BotFather. The terminal hides the input. Keep this terminal open until webhook registration is complete.

```bash
read -rsp 'Bot token: ' BOT_TOKEN; printf '\n'
```

Before registering the webhook, send a private message to the new bot from each account that should be allowed to report. Then run:

```bash
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates"
```

Find each message under `result` and copy its `message.from.id`. For anonymous administrators, send a message mentioning the bot from the group's anonymous identity and copy `message.sender_chat.id` instead. If the result is empty, send a fresh message and run the command again. Do not publish this output; it contains member information.

Save these settings as Worker secrets. Each `secret put` command prompts for its value. If Wrangler asks to create the Worker, confirm:

```bash
mise exec -- uv run pywrangler secret put BOT_USERNAME
mise exec -- uv run pywrangler secret put REPORTER_IDS
printf '%s' "$BOT_TOKEN" | mise exec -- uv run pywrangler secret put BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET="$(mise exec -- python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s' "$TELEGRAM_WEBHOOK_SECRET" | mise exec -- uv run pywrangler secret put TELEGRAM_WEBHOOK_SECRET
```

| Setting | Value |
| --- | --- |
| `BOT_USERNAME` | Your moderation bot's username, without `@`. |
| `REPORTER_IDS` | Trusted numeric user or sender-chat IDs, separated by commas. For example: `123456789,-1001234567890`. Replace both example IDs. |

Only listed identities can report or use `/bs`, even if they own a group. For messages sent as a group or channel, authorization uses that sending identity, not the personal account behind it. Listing a group does not authorize its members' personal accounts. Leave the list empty to disable reports and `/bs`.

Keep tokens and secrets out of repository files, screenshots, and public logs.

## 5. Deploy and connect Telegram

```bash
mise exec -- uv run pywrangler deploy
```

Copy the Worker HTTPS URL printed by the command. Register it as Telegram's webhook, the address where Telegram sends messages to the bot:

```bash
read -rp 'Worker HTTPS URL: ' WORKER_URL
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WORKER_URL%/}/webhook" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","edited_message"]'
unset TELEGRAM_WEBHOOK_SECRET
```

Confirm that the response contains `"ok":true` and `"result":true`. Use this token with only one bot deployment; registering another webhook replaces this one.

## Report spam and manage sources

### Report a message

Using an identity listed in `REPORTER_IDS`, choose **Reply** on the spam message, type `@`, select your moderation bot, and send.

In a supergroup, a report from an administrator or an authorized group/channel identity deletes the target message, bans its sender, and clears eligible indexed history. Human and bot accounts follow the same policy, with group owners and administrators protected from bans. The bot removes the report after cleanup succeeds. A listed ordinary member's report saves evidence without deleting messages or restricting anyone.

Confirmed bans add the sender to the account blacklist. When that account posts in another supergroup using this bot, the bot bans it there and clears eligible indexed messages.

### Add a source

Send `/bs @example_bot` using a listed reporting identity, in private or in a group. Replace `example_bot` with the source bot's username. In groups, `/bs@your_moderation_bot @example_bot` addresses this moderation bot explicitly.

The bot saves the resolved account ID in the source list and replies with that ID. It removes the group command after replying, including when lookup fails; private-chat commands and result replies remain. Registration itself does not ban the account or delete its history. Check the returned ID: Telegram cannot resolve every username, and the command does not check whether the account is a bot.

When someone sends an inline message through a listed source bot, or forwards a message with that bot as the visible origin:

- The sender loses only that message and is permanently muted. Their other messages remain, and they are not added to a blacklist.
- The source bot is banned, added to the account blacklist, and its eligible indexed messages are cleared.

Copied text, hidden forwarding origins, and messages sent directly by an ordinary account do not match the source list.

### Administrator protection and history limits

Group owners and administrators are exempt from mutes and bans, including when the source bot is an administrator. Automatic filtering leaves their messages intact. An authorized administrator report can still delete a targeted administrator message.

History cleanup covers indexed messages received in the same group, up to the triggering message or before the report, and less than 48 hours old. The bot cannot search unread history. Telegram may remove additional history when banning an account.

### Inspect lists and remove a false positive

In the Cloudflare dashboard, open **D1 → anti-fwd-spam-reports → Console**. Inspect the lists:

```sql
SELECT source_id FROM blacklisted_sources ORDER BY source_id;
SELECT bot_id, user_id FROM blacklisted_users ORDER BY bot_id, user_id;
```

`blacklisted_sources` contains registered source IDs; `blacklisted_users` contains banned account IDs for each moderation bot. Copy the IDs to remove and replace the examples below. A source bot can appear in both lists; remove it from both to stop both kinds of filtering:

```sql
DELETE FROM blacklisted_sources WHERE source_id = 987654321;
DELETE FROM blacklisted_users WHERE bot_id = 123456789 AND user_id = 987654321;
```

Then lift any mute or ban in each affected group's Telegram member settings. Removing a database entry does not lift Telegram restrictions. An account left on the account blacklist will be banned again when it posts.

## Automatic text filtering

Local rules check new group messages before Jev, including messages from bots, without an API key. They search anywhere in text or captions for campaign markers and runs of identical money-bag or red-circle emojis, allowing whitespace between emojis. Surrounding text or other emojis do not prevent a match. The exact patterns are `SPAM_PATTERNS` in [policy.py](src/anti_fwd_spam/policy.py). The contents of replied-to messages and individual sensitive words do not trigger these rules.

A match deletes the current message and permanently mutes a human sender in a supergroup, preserving earlier messages and both blacklists. Bot senders are not muted. Group owners and administrators are protected. Private messages and edits skip this check.

## Optional: enable Jev spam checks

Model checks send the sender's display name, available biography, and message text or caption with formatting and selected media descriptions to an external provider. No media is downloaded or inspected. Review the provider's privacy terms and pricing before enabling this feature.

Save an API key under the corresponding Worker secret. With multiple keys, the bot starts with the first configured provider below and can switch providers on failures:

| Provider | Worker secret |
| --- | --- |
| [TypeSafe AI](https://ai-sdk.dev/providers/ai-sdk-providers/typesafe-ai) | `TYPESAFE_AI_API_KEY` |
| [Vercel AI Gateway](https://vercel.com/ai-gateway/models/jev) | `AI_GATEWAY_API_KEY` |
| [Experiential](https://platform.experientiallabs.ai/models/jev-latest) | `EXPERIENTIAL_API_KEY` |
| [OpenCode Zen](https://opencode.ai/docs/zen/) | `OPENCODE_API_KEY` |

For Vercel, run:

```bash
mise exec -- uv run pywrangler secret put AI_GATEWAY_API_KEY
```

Jev checks new user messages after the other moderation rules. A sufficiently high spam score deletes that message and permanently mutes its sender, preserving their earlier messages and leaving both blacklists unchanged. Group owners and administrators are protected. The threshold is `SPAM_THRESHOLD` in [model.py](src/anti_fwd_spam/model.py); a score is not an accuracy guarantee. Edited messages, service events, bot senders, and messages sent as a group or channel skip this check.

Temporary model failures leave the message visible while scheduled retries run. Temporary deletion and mute failures also retry; a deleted message stays deleted while a mute is pending. Keep the scheduled trigger enabled. Each model request can incur charges and can send the same content to another configured provider.

To disable checks and pause pending model tasks, delete every configured model secret. For a Vercel-only setup:

```bash
mise exec -- uv run pywrangler secret delete AI_GATEWAY_API_KEY
```

## Check operation and troubleshoot

Use a disposable test group and account. Report a test message from an authorized administrator and confirm deletion and the sender's ban. Test source filtering separately: add a source from a private chat, send an inline message from that source using a non-admin account, and confirm deletion and muting while earlier messages remain. Remove test blacklist entries and restrictions when finished.

Check Telegram delivery in the terminal holding `BOT_TOKEN`:

```bash
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

`result.url` must equal the Worker URL plus `/webhook`. `pending_update_count: 0` means no backlog; it does not prove moderation works. If the count keeps growing or a new `last_error_message` appears, open the Worker's logs in the Cloudflare dashboard.

| Symptom | Check |
| --- | --- |
| Reports or `/bs` do nothing | Check `REPORTER_IDS`, the sending identity, and `BOT_USERNAME`. |
| Webhook returns `401` | Save and register the same `TELEGRAM_WEBHOOK_SECRET`. |
| Webhook returns `404` | Use the deployment URL ending in `/webhook`. |
| Deletion, mute, or ban fails | Check the bot's administrator permissions and whether the target is protected. |
| `ban confirmation failed` or `mute retry skipped` | Check membership manually before sending a new report or changing restrictions. |
| `history cleanup rejected` | Check and delete remaining messages manually. |
| Storage errors | Check the `REPORTS` binding, database tables, and D1 usage. |

In a fresh terminal, re-enter `BOT_TOKEN` using the command in step 4. Run `unset BOT_TOKEN` when finished.

## Data storage

D1 retains report and source-moderation JSON, resolved command IDs, and temporary processing records for 3 days. The recent-message index stores IDs and timestamps, not message text. Model tasks retain submitted content while classification is pending, then keep only the identities needed to finish deletion and muting. The bot does not download media files.

Source and account blacklists remain until manually removed. Scheduled cleanup removes expired records; outages or exhausted quotas can delay it. Cloudflare backups have separate [retention rules](https://developers.cloudflare.com/d1/reference/time-travel/). Keep member information private when viewing or exporting the database.
