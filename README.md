# anti-fwd-spam

English | [简体中文](README_zh-Hans.md)

Deploy this Telegram moderation bot to your Cloudflare account to filter messages from specified source bots and handle spam reports.

## Create the bot and add it to a group

1. Send `/newbot` to [@BotFather](https://t.me/BotFather), follow the prompts, and save the token and username.
2. Disable Group Privacy Mode in BotFather. To receive messages from other bots, enable Bot-to-Bot Communication Mode using [Telegram's instructions](https://core.telegram.org/api/bots/bot-to-bot).
3. Add the bot to your group as an administrator with permission to delete messages and ban users.
4. For channel comments, add the bot to the channel's linked discussion group. Muting and report-based bans apply to supergroups, including channel discussion groups. In basic groups, the bot only deletes messages.

## Deploy to Cloudflare

### Install the tools and sign in

Create a Cloudflare account and install [Git](https://git-scm.com/downloads) and [mise](https://mise.jdx.dev/getting-started.html). Run these commands in Bash. Use the same terminal for credentials and webhook registration.

```bash
git clone https://github.com/IceCodeNew/anti-fwd-spam.git
cd anti-fwd-spam
mise trust
mise install
mise exec -- uv sync --locked
mise exec -- npm ci --ignore-scripts
mise exec -- uv run pywrangler login
```

To avoid paid usage, keep the Cloudflare Free plan and monitor usage in the dashboard. Check the current [Workers](https://developers.cloudflare.com/workers/platform/pricing/) and [D1](https://developers.cloudflare.com/d1/platform/pricing/) limits before deploying.

### Configure source bots and the database

Edit `vars` in [`wrangler.jsonc`](wrangler.jsonc):

| Setting | Value |
| --- | --- |
| `BLACKLIST_BOT_IDS` | Comma-separated numeric IDs of source bots to block. Do not use group IDs, ordinary user IDs, or usernames. |
| `BOT_USERNAME` | Your moderation bot's username without `@`. Update this setting if you rename the bot. |

Create the report database:

```bash
mise exec -- uv run pywrangler d1 create anti-fwd-spam-reports
```

Replace the repository's `database_id` in `wrangler.jsonc` with the ID returned by that command. Keep `binding` set to `REPORTS`, then apply the migrations:

```bash
mise exec -- uv run pywrangler d1 migrations apply anti-fwd-spam-reports --remote
```

For an existing deployment, keep its database ID and skip database creation.

### Save credentials and deploy

Enter the token from BotFather. The terminal hides your input:

```bash
read -rsp 'Bot token: ' BOT_TOKEN; printf '\n'
TELEGRAM_WEBHOOK_SECRET="$(mise exec -- python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s' "$BOT_TOKEN" | mise exec -- uv run pywrangler secret put BOT_TOKEN
printf '%s' "$TELEGRAM_WEBHOOK_SECRET" | mise exec -- uv run pywrangler secret put TELEGRAM_WEBHOOK_SECRET
mise exec -- uv run pywrangler deploy
```

Save the HTTPS URL returned by the deployment command. Keep the bot token and webhook secret out of the repository, screenshots, and public logs.

### Register the webhook

Enter the deployment URL as `WORKER_URL`, without a trailing `/`. This replaces the bot's existing webhook. Do not share its token with another bot service.

```bash
read -rp 'Worker HTTPS URL: ' WORKER_URL
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WORKER_URL%/}/webhook" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","edited_message"]'
unset TELEGRAM_WEBHOOK_SECRET
```

Check that both `ok` and `result` are `true` in the response. For later rule or code changes, apply pending migrations with the [database migration command](#configure-source-bots-and-the-database), then run `mise exec -- uv run pywrangler deploy`. Register the webhook again when its URL, secret, or `allowed_updates` changes.

## Report spam

1. Long-press or right-click the target message and choose **Reply**.
2. Type `@`, select your moderation bot from Telegram's suggestions, and send the reply.

Reports from ordinary members only save evidence; both messages remain in the group. An owner or administrator's report permanently bans a non-admin sender in a supergroup, preventing comments and rejoining until an administrator unbans them. The bot deletes the reported message and clears that sender's indexed messages in the same group, sent before the report and less than 48 hours ago. It processes up to 100 indexed messages per delivery and continues on retries. Check the reply target before reporting: unbanning does not restore deleted messages.

Cleanup covers messages the bot received while indexing was active. The bot cannot search older history or recover missed updates. Telegram's supergroup ban API always enables history revocation, even without an explicit request parameter; the bot cannot verify cleanup outside its index. Check for remaining messages and delete them manually if needed.

The bot removes the administrator's report after confirming target deletion and finishing eligible history cleanup. If target deletion is rejected, the report stays visible and the log includes `deletion rejected; banned` when the ban succeeded; eligible recent history is still processed. `history cleanup rejected` means Telegram rejected a batch. Check the remaining messages manually. Anonymous administrators can report while sending as the group itself. Reports sent as a linked channel or another chat are ignored.

After a saved ban confirmation, retries after a temporary deletion failure continue deletion without banning again. If logs show `ban confirmation failed`, the bot could not confirm the result and will not repeat the ban for that report. Check the sender's membership; if moderation is still needed, send a new report or ban them in Telegram's member settings. The bot leaves the unconfirmed report visible for this check.

Source-bot matches delete only the matching message and permanently mute the sender, preserving other history and group membership. For target owners, administrators, or senders acting as a group or channel, the bot only deletes the target message; it does not mute or ban them.

Source-bot filtering requires Telegram's explicit inline-bot or forwarded-bot provenance. Report copied text or forwarded messages with hidden origins manually, or enable the optional model check below.

## Enable model-based spam detection

Save an [Experiential](https://platform.experientiallabs.ai/models/jev-latest) API key as a **Worker secret**, then deploy the code:

```bash
mise exec -- uv run pywrangler secret put EXPERIENTIAL_API_KEY
mise exec -- uv run pywrangler deploy
```

The bot sends each eligible new group message to `jev-latest`, combining the sender's display name, available biography, and message text or caption with formatting and selected media descriptors. This shares member content with Experiential; review the provider's privacy terms and pricing before enabling it. The bot does not send replied-to messages, conversation history, or media file IDs, and does not download or inspect media. Unavailable biographies are marked unknown. A successful lookup with no biography is marked empty.

The bot deletes only the current message when the returned spam probability is at least 0.95. This score is a model estimate, not a guarantee of 95% accuracy. Model decisions do not mute, ban, add accounts to the blacklist, or clear history. Existing account-blacklist, source-bot, and report processing take priority. Edits, service messages, bot senders, and messages sent as channels or anonymous administrators skip the model check.

Biography lookup and inference each have an eight-second deadline. Model failures or invalid answers retain the message and acknowledge delivery; failed biography lookup still permits classification. Temporary deletion failures request webhook redelivery, which may classify the message again and incur another API charge. The bot stores no model score or biography. Check `model check failed`, `model deletion rejected`, and `model deletion pending retry` in Worker logs. Remove the Worker secret to disable model checks:

```bash
mise exec -- uv run pywrangler secret delete EXPERIENTIAL_API_KEY
```

Start in a disposable group with ordinary discussion, promotional messages, and quoted scam warnings. Check that deleted messages leave membership and earlier history unchanged. Inspect Worker CPU usage and errors in Cloudflare; external network waits do not count as CPU time, but local execution still consumes the plan's CPU allowance. A key in a development terminal or Amp project does not configure the deployed Worker's secret.

## Block reported accounts across groups

After Telegram confirms an administrator-authorized report ban, the bot saves the account in its database blacklist. Ordinary-member reports, automatic mutes, and unconfirmed bans do not add accounts. Only add the bot to groups whose administrators you trust: a confirmed report in any such group affects other groups using the same bot.

For every new supergroup message from a real user, the bot checks the sender against its account blacklist. A match takes priority over source-bot filtering. The bot bans the sender unless already banned, then batch-deletes only eligible indexed messages through the triggering message. The triggering message enters the index if eligible. The bot makes no separate single-message deletion or history-search attempts. Owners and administrators retain their membership and messages. Basic groups and edits do not trigger account-blacklist enforcement. Each check adds a D1 read, so monitor database usage.

The bot acts when a blacklisted account posts a new message. Adding or promoting the bot does not ban anyone in advance. Telegram may revoke other history as a side effect of a supergroup ban; its API does not offer an opt-out.

Duplicate deliveries do not repeat a confirmed ban while its three-day processing record exists. A new message from an account still on the blacklist triggers another ban even after a manual unban. Remove false positives from the database as well as unbanning them in Telegram. Check membership manually if logs show `ban confirmation failed`; uncertain outcomes do not trigger another ban on redelivery. Temporary explicit rejections remain retryable. `history cleanup rejected` requires a manual check of remaining indexed messages.

## Verify in a test group

In a test supergroup or channel discussion group, use a non-admin account to send a normal message followed by an inline message from a blacklisted source bot. Check that only the inline message disappears, the account cannot send more messages, and its earlier message remains.

Remove the account's restriction in the group's member settings, then send several messages and a sticker while the bot is running. Report the sticker first from an ordinary member account and check that the messages remain. Report it from an administrator account and check that the target, earlier indexed messages and report disappear, the sender cannot comment through the linked channel, and they cannot rejoin through an invite link. Other users' messages and messages in other groups should remain.

Repeat the administrator report with **Remain Anonymous** enabled and send as the group itself. Also test a commenter who has not joined the discussion group. Check target deletion and both commenting and rejoining restrictions.

To check administrator protection, report a message from an administrator. Only that message should disappear; the administrator should still be able to send messages and retain their other history. Remove test restrictions or bans after each round. Use disposable accounts and groups for tests that delete history.

After a confirmed report ban, add the bot as an administrator to another disposable supergroup. Check that joining alone does not ban the test account. Send a new message from that account, then check that it disappears and the account cannot rejoin. Remove the test account from the database blacklist using the instructions below, unban it, and confirm that a new message remains visible.

## Check the webhook and failures

Run this in the terminal holding `BOT_TOKEN`. In a new terminal, enter the token again with the `read -rsp` command above.

```bash
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

Check that `result.url` matches the deployment URL plus `/webhook`. Query again after sending a test message. If `pending_update_count` keeps growing or a new `last_error_message` appears, open the Worker's logs in the Cloudflare dashboard.

- For `401`, save the webhook secret again and register the webhook with the same value.
- For `404`, check that the URL ends in `/webhook`.
- For deletion, mute, or ban failures, check the bot's administrator permissions and whether the target is an administrator or anonymous sender. `report cleanup rejected` means report deletion was rejected; read the same log entry for the target's moderation outcome. Telegram retries `report cleanup pending` responses.
- For `mute retry skipped`, check the sender's permissions manually if moderation is still needed. The bot does not repeat a potentially successful mute for the same message while its deduplication record exists, including edited updates, so redelivery does not undo an administrator's unmute. Explicit temporary Telegram rejections remain retryable; a new matching message can trigger another mute.
- For D1 storage failures, including `account moderation storage unavailable`, check the `REPORTS` binding, database migrations, and D1 usage in the Cloudflare dashboard. If mute storage is unavailable, automatic filtering can still delete the target, but muting waits for storage recovery. `history cleanup pending retry` means more batches remain or a temporary error interrupted cleanup; the bot keeps the report visible until processing finishes.

`pending_update_count: 0` only means there is no backlog. Verify message and membership changes in the test group. Run `unset BOT_TOKEN` when finished.

## View stored data

Open the `reports` table in the `anti-fwd-spam-reports` D1 database in the Cloudflare dashboard. `raw_update` contains Telegram's complete report JSON, including the replied-to message. `classification` contains content-type and media-field classifications. Keep member information private when viewing or exporting records.

Reports are retained for 3 days. Account-blacklist message matches also save their complete update JSON and processing progress in `reports` for 3 days, allowing deletion retries without repeating a confirmed ban. The bot does not download images or other media. Scheduled cleanup removes expired records; service failures or exhausted quotas can delay deletion. Cloudflare backups have separate retention rules; see [D1 Time Travel](https://developers.cloudflare.com/d1/reference/time-travel/).

The `recent_messages` table stores only bot, group, sender and message IDs plus the original sending time for observed user messages in supergroups. It stores no message text or media. Edits do not renew retention. Identifiers stop qualifying for deletion after 48 hours; scheduled cleanup removes expired rows in bounded batches and can lag behind a backlog. Successful batch deletion removes identifiers sooner. Indexing adds D1 writes for received messages, so monitor database usage even when nobody reports spam.

The `automatic_mutes` table stores bot, group and message IDs with an expiry time to prevent repeated automatic restrictions. It stores no content. Each record expires 3 days after creation; duplicate or edited updates do not refresh an existing record's expiry. Scheduled cleanup removes expired rows in bounded batches.

The `blacklisted_users` table retains bot/user IDs and the time of the first confirmed report ban until manually removed. Report expiry does not remove these accounts.

To stop future blacklist matches for a false positive, open the D1 console and run the following SQL after replacing both numeric IDs. Then unban the account in each affected group's Telegram settings; deleting database records alone does not lift Telegram bans.

```sql
DELETE FROM blacklisted_users WHERE bot_id = 123456789 AND user_id = 987654321;
```
