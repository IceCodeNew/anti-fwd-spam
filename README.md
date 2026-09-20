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

Set `vars.BOT_USERNAME` in [`wrangler.jsonc`](wrangler.jsonc) to your moderation bot's username without `@`. Update it if you rename the bot. Source IDs are stored in D1's `blacklisted_sources` table.

Create the report database:

```bash
mise exec -- uv run pywrangler d1 create anti-fwd-spam-reports
```

Replace the repository's `database_id` in `wrangler.jsonc` with the ID returned by that command. Keep `binding` set to `REPORTS`, then apply the migrations:

```bash
mise exec -- uv run pywrangler d1 migrations apply anti-fwd-spam-reports --remote
```

For an existing deployment, keep its database ID and skip database creation. Apply migrations before deploying the code. A new database starts with an empty source list; use `/ban` to add sources. Additions survive deployments and evidence expiry.

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

Configure the reporting identities as a runtime Worker secret:

```bash
mise exec -- uv run pywrangler secret put REPORTER_IDS
```

Enter comma-separated numeric user and sender chat IDs in `REPORTER_IDS`. The bot checks `sender_chat.id` for messages sent as a chat, or the real sender's `from.id` otherwise. A match accepts the report. A sender chat need not match the destination group. Listing a chat does not authorize members reporting under their personal accounts; add their user IDs to the same list. For messages sent as a chat, Telegram's compatibility `from` user never grants access.

With the list empty or absent, the bot ignores reports. An unlisted reporter cannot save evidence, add accounts to the blacklist, or trigger report-based deletion or punishment, even as a group owner. Removing an identity also stops its pending report retries. Automatic filtering and existing account-blacklist enforcement remain active.

1. Long-press or right-click the target message and choose **Reply**.
2. Type `@`, select your moderation bot from Telegram's suggestions, and send the reply.

Reports from ordinary members only save evidence; both messages remain in the group. An owner or administrator's report permanently bans a non-admin sender in a supergroup, preventing comments and rejoining until an administrator unbans them. The bot deletes the reported message and clears that sender's indexed messages in the same group, sent before the report and less than 48 hours ago. It processes up to 100 indexed messages per delivery and continues on retries. Check the reply target before reporting: unbanning does not restore deleted messages.

Cleanup covers messages the bot received while indexing was active. The bot cannot search older history or recover missed updates. Telegram's supergroup ban API always enables history revocation, even without an explicit request parameter; the bot cannot verify cleanup outside its index. Check for remaining messages and delete them manually if needed.

The bot removes the administrator's report after confirming target deletion and finishing eligible history cleanup. If target deletion is rejected, the report stays visible and the log includes `deletion rejected; banned` when the ban succeeded; eligible recent history is still processed. `history cleanup rejected` means Telegram rejected a batch. Check the remaining messages manually. Reports from an allowlisted sender chat, including anonymous administrators, receive administrator report handling.

After a saved ban confirmation, retries after a temporary deletion failure continue deletion without banning again. If logs show `ban confirmation failed`, the bot could not confirm the result and will not repeat the ban for that report. Check the sender's membership; if moderation is still needed, send a new report or ban them in Telegram's member settings. The bot leaves the unconfirmed report visible for this check.

Source-bot matches delete only the matching message and permanently mute the sender, preserving other history and group membership. For target owners, administrators, or senders acting as a group or channel, the bot only deletes the target message; it does not mute or ban them.

Source-bot filtering requires Telegram's explicit inline-bot or forwarded-bot provenance. Report copied text or forwarded messages with hidden origins manually, or enable the optional model check below.

## Add a source by username

Using an identity listed in `REPORTER_IDS`, send `/ban example_bot` or `/ban @example_bot`. In groups, `/ban@your_moderation_bot example_bot` explicitly addresses your bot. The bot replies to the command with the resolved numeric ID and processing outcome. It accepts resolvable account usernames without checking whether the account is a bot.

In a supergroup, the command also bans the resolved user account and clears its indexed history through the command message, within Telegram's 48-hour deletion window. Owners and administrators remain protected. Already-banned accounts stay banned; the command does not unban or apply a mute that could replace their ban. Confirmed bans also enter the account blacklist. An uncertain ban result requires a membership check before submitting a new command.

In a private chat or basic group, the command only adds the source ID. A resolved group or channel ID is stored as a source but is not passed to the user-ban API. Existing provenance rules remain unchanged: storing an ordinary user or chat ID does not make their direct messages match the source filter.

The source list does not expire. To inspect it, run:

```bash
mise exec -- uv run pywrangler d1 execute anti-fwd-spam-reports --remote --command 'SELECT source_id FROM blacklisted_sources ORDER BY source_id;'
```

Command evidence and the resolved ID are retained for 3 days. Redelivery uses the saved ID even if the username changes. Edited commands and unauthorized commands do not add entries. Telegram may deliver duplicate replies when a reply's delivery cannot be confirmed.

## Enable model-based spam detection

Save your provider's API key as a **Worker secret**, then deploy the code. The bot selects providers from nonempty secrets in this order; no platform setting is needed:

| Provider | Worker secret |
| --- | --- |
| [TypeSafe AI](https://ai-sdk.dev/providers/ai-sdk-providers/typesafe-ai) | `TYPESAFE_AI_API_KEY` |
| [Vercel AI Gateway](https://vercel.com/ai-gateway/models/jev) | `AI_GATEWAY_API_KEY` |
| [Experiential](https://platform.experientiallabs.ai/models/jev-latest) | `EXPERIENTIAL_API_KEY` |
| [OpenCode Zen](https://opencode.ai/docs/zen/) | `OPENCODE_API_KEY` |

For example, to use Vercel:

```bash
mise exec -- uv run pywrangler secret put AI_GATEWAY_API_KEY
mise exec -- uv run pywrangler deploy
```

OpenCode uses Zen, not Go. Check account verification and billing requirements with each provider. Endpoints and model IDs are defined in `MODEL_PROVIDERS` in [`src/anti_fwd_spam/model.py`](src/anti_fwd_spam/model.py).

The bot sends each eligible new group message to Jev, combining the sender's display name, available biography, and message text or caption with formatting and selected media descriptors. With multiple keys, retries can share the same input with multiple providers; review their privacy terms and pricing before enabling them. The bot does not send replied-to messages, conversation history, or media file IDs, and does not download or inspect media. Unavailable biographies are marked unknown. A successful lookup with no biography is marked empty.

The bot deletes only the current message when the returned spam probability is at least 0.95. This score is a model estimate, not a guarantee of 95% accuracy. Model decisions do not mute, ban, add accounts to the blacklist, or clear history. Existing account-blacklist, source-bot, and report processing take priority. Edits, service messages, bot senders, and messages sent as channels or anonymous administrators skip the model check.

Biography lookup and inference each have an eight-second deadline; failed biography lookup still permits classification. Network failures, timeouts, and HTTP 408, 429 or 5xx responses keep the message visible and schedule retries after 1, 2 and 5 minutes, rotating through configured providers and wrapping around if needed. The four-attempt limit is shared across providers. A permanent HTTP rejection only schedules another attempt if a provider remains untried in the initial rotation. Invalid answers or valid scores below the threshold stop processing without consulting another provider. Each inference attempt can incur an API charge. Once classification succeeds, temporary deletion failures retry deletion without another model request.

Keep the Worker's Cron Trigger enabled. It processes one due message per minute, so a backlog can delay retries. Processing stops when a message reaches 48 hours old. An edited update cancels pending work for the original content; a deletion already sent to Telegram cannot be recalled. Pending inference uses the keys configured when it runs; changing keys can skip or revisit a provider without resetting the attempt budget. Keep the configured key set unchanged while tasks are pending to preserve rotation order. Remove every configured model secret to disable new checks and pause pending tasks, including deletion. For a Vercel-only deployment:

```bash
mise exec -- uv run pywrangler secret delete AI_GATEWAY_API_KEY
```

Start in a disposable group with ordinary discussion, promotional messages, and quoted scam warnings. Check that deleted messages leave membership and earlier history unchanged. Inspect Worker CPU usage and errors in Cloudflare; external network waits do not count as CPU time, but local execution still consumes the plan's CPU allowance. A key in a development terminal or Amp project does not configure the deployed Worker's secret.

## Block reported accounts across groups

After Telegram confirms an administrator-authorized report ban, the bot saves the account in its database blacklist. Ordinary-member reports, automatic mutes, and unconfirmed bans do not add accounts. Only authorize trusted [reporting identities](#report-spam): their confirmed bans affect other groups using the same bot.

For every new supergroup message from a real user, the bot checks the sender against its account blacklist. A match takes priority over source-bot filtering. The bot bans the sender unless already banned, then batch-deletes only eligible indexed messages through the triggering message. The triggering message enters the index if eligible. The bot makes no separate single-message deletion or history-search attempts. Owners and administrators retain their membership and messages. Basic groups and edits do not trigger account-blacklist enforcement. Each check adds a D1 read, so monitor database usage.

The bot acts when a blacklisted account posts a new message. Adding or promoting the bot does not ban anyone in advance. Telegram may revoke other history as a side effect of a supergroup ban; its API does not offer an opt-out.

Duplicate deliveries do not repeat a confirmed ban while its three-day processing record exists. A new message from an account still on the blacklist triggers another ban even after a manual unban. Remove false positives from the database as well as unbanning them in Telegram. Check membership manually if logs show `ban confirmation failed`; uncertain outcomes do not trigger another ban on redelivery. Temporary explicit rejections remain retryable. `history cleanup rejected` requires a manual check of remaining indexed messages.

## Verify in a test group

In a test supergroup or channel discussion group, use a non-admin account to send a normal message followed by an inline message from a blacklisted source bot. Check that only the inline message disappears, the account cannot send more messages, and its earlier message remains.

Add the testing reporters to `REPORTER_IDS`. Remove the account's restriction in the group's member settings, then send several messages and a sticker while the bot is running. Report the sticker first from an ordinary member account and check that the messages remain. Report it from an administrator account and check that the target, earlier indexed messages and report disappear, the sender cannot comment through the linked channel, and they cannot rejoin through an invite link. Other users' messages and messages in other groups should remain.

Repeat the administrator report with **Remain Anonymous** enabled after adding its sender chat ID to `REPORTER_IDS`. Verify that an unlisted owner and an unlisted sender chat cannot report. Also test a commenter who has not joined the discussion group. Check target deletion and both commenting and rejoining restrictions.

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

The `model_tasks` table temporarily stores the model input (nickname, available biography and selected message content) with identifiers and processing progress. The bot clears the input after classification succeeds or processing stops, including edits and exhausted retries. Deduplication metadata expires 3 days after task creation; retries never extend that date. Scheduled cleanup also clears unfinished content after the message reaches 48 hours old. To inspect pending work without exposing member content, run `SELECT phase, COUNT(*) FROM model_tasks GROUP BY phase;` in the D1 console. Apply pending database migrations before enabling model checks.

The `blacklisted_users` table retains bot/user IDs and the time of the first confirmed report ban until manually removed. Report expiry does not remove these accounts.

To stop future blacklist matches for a false positive, open the D1 console and run the following SQL after replacing both numeric IDs. Then unban the account in each affected group's Telegram settings; deleting database records alone does not lift Telegram bans.

```sql
DELETE FROM blacklisted_users WHERE bot_id = 123456789 AND user_id = 987654321;
```
