# anti-fwd-spam

English | [简体中文](README_zh-Hans.md)

A Telegram moderation bot hosted on Cloudflare Workers, with blocklists and report records stored in Cloudflare D1. Use it to report spam, block messages from specified source bots, and optionally check new messages with the Jev model.

Start in a test group. Bans can delete message history, and unbanning cannot restore deleted messages.

## 1. Create a Telegram bot

1. Send `/newbot` to [@BotFather](https://t.me/BotFather) and follow the prompts. Save the bot token and username.
2. In BotFather, disable Group Privacy Mode. To receive messages from other bots, also enable [Bot-to-Bot Communication Mode](https://core.telegram.org/api/bots/bot-to-bot).
3. Add the bot to your group as an administrator with permission to delete messages and ban users. For a channel's comments, use its linked discussion group.

Bans and mutes require a supergroup, which includes channel discussion groups. Basic groups support message deletion only.

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

Save these settings as Worker secrets. Each `secret put` command prompts for its value:

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

To report a message, choose **Reply**, type `@`, select your moderation bot from Telegram's suggestions, and send. Check the reply target before sending.

| Action | Result in a supergroup |
| --- | --- |
| A listed ordinary member reports | Save evidence; leave both messages visible. |
| A listed administrator or sender-chat identity reports | Delete the target, ban its non-admin sender, and clear eligible indexed history. Remove the report after cleanup succeeds. |
| A user posts through a blocked source bot | Delete the matching message and permanently mute its non-admin sender, retaining other history and membership. |
| An account on the account blacklist posts | Ban it unless already banned and clear eligible indexed messages. |

Group owners and administrators are exempt from mutes and bans. Their reported messages or source-matched messages can still be deleted. Account-blacklist matches leave administrators' messages intact.

History cleanup covers messages received by the bot in the same group, up to the triggering message or before the report, and less than 48 hours old. The bot cannot search unread history. Telegram may remove additional history when banning an account. After an administrator-authorized report ban is confirmed, the actual sender enters `blacklisted_users`, shared across groups using the same bot. Reporting a message does not add its source bot to the source list.

### Add a source

Send `/bs @example_bot` using a listed reporting identity, in a private chat or group. In a group, use `/bs@your_moderation_bot @example_bot` to address this bot explicitly. Replace the example usernames. The bot saves the resolved ID in `blacklisted_sources` and replies with that ID.

The command only adds a source. It does not delete messages, change membership, or add an account-blacklist entry. Future messages whose inline bot (`via_bot.id`) or forwarded bot matches the source list trigger the source-filtering policy above.

Username resolution depends on Telegram. Some accounts cannot be resolved; an unsuccessful lookup adds nothing. The command does not require the target to be a bot. Source matching applies only to Telegram's explicit inline-bot or forwarded-bot information; adding an ordinary user or group ID does not block their direct messages. Copied text and hidden forwarding origins do not match this filter.

### Inspect lists and remove a false positive

In the Cloudflare dashboard, open **D1 → anti-fwd-spam-reports → Console**. Inspect the lists:

```sql
SELECT source_id FROM blacklisted_sources ORDER BY source_id;
SELECT bot_id, user_id FROM blacklisted_users ORDER BY bot_id, user_id;
```

To remove an entry, copy its IDs from the results and replace the examples below. Run only the statement for the list being corrected:

```sql
DELETE FROM blacklisted_sources WHERE source_id = 987654321;
DELETE FROM blacklisted_users WHERE bot_id = 123456789 AND user_id = 987654321;
```

Then lift any mute or ban in each affected group's Telegram member settings. Removing a database entry does not lift Telegram restrictions. An account left on the account blacklist will be banned again when it posts.

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

Jev checks eligible new user messages after the other moderation rules. A spam score of at least 0.95 deletes only that message, without muting, banning, or adding a blacklist entry. The score is an estimate, not an accuracy guarantee. Edited messages, service events, bot senders, and messages sent as a group or channel skip this check.

Temporary failures leave the message visible and schedule retries after 1, 2 and 5 minutes. Keep the scheduled trigger enabled; a backlog can delay retries. Each attempt can incur charges and can send the same content to another configured provider.

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

D1 stores report JSON, including the reported message, and source-command records with their resolved IDs for 3 days. Temporary processing records also expire after 3 days. The bot indexes message IDs and timestamps for recent-history cleanup without storing message text in that index. Model tasks temporarily retain the content submitted for classification. The bot does not download media files.

Source and account blacklists remain until manually removed. Scheduled cleanup removes expired records; outages or exhausted quotas can delay it. Cloudflare backups have separate [retention rules](https://developers.cloudflare.com/d1/reference/time-travel/). Keep member information private when viewing or exporting the database.
