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

Check that both `ok` and `result` are `true` in the response. For later rule or code changes, apply pending migrations with the [database migration command](#configure-source-bots-and-the-database), then run `mise exec -- uv run pywrangler deploy`. Register the webhook again only if its URL or secret changes.

## Report spam

1. Long-press or right-click the target message and choose **Reply**.
2. Type `@`, select your moderation bot from Telegram's suggestions, and send the reply.

Reports from ordinary members only save evidence; both messages remain in the group. An owner or administrator's report permanently bans a non-admin sender in a supergroup, preventing comments and rejoining until an administrator unbans them. The bot separately deletes the reported message and requests deletion of the sender's history through Telegram's ban API. History cleanup is not independently confirmed; check for remaining messages and delete them manually if needed. Check the reply target before reporting: unbanning does not restore deleted messages.

The bot removes the administrator's report only after Telegram confirms that the target was deleted or is already absent. If target deletion is rejected, the report stays visible and the log says `deletion rejected; banned` when the ban succeeded. Delete the remaining message manually. Anonymous administrators can report while sending as the group itself. Reports sent as a linked channel or another chat are ignored.

After a saved ban confirmation, retries after a temporary deletion failure continue deletion without banning again. If logs show `ban confirmation failed`, the bot could not confirm the result and will not repeat the ban for that report. Check the sender's membership; if moderation is still needed, send a new report or ban them in Telegram's member settings. The bot leaves the unconfirmed report visible for this check.

Automatic matches delete only the matching message and permanently mute the sender, preserving other history and group membership. For target owners, administrators, or senders acting as a group or channel, the bot only deletes the target message; it does not mute or ban them.

Automatic filtering requires Telegram's explicit inline-bot or forwarded-bot provenance. Report copied text or forwarded messages with hidden origins manually.

## Verify in a test group

In a test supergroup or channel discussion group, use a non-admin account to send a normal message followed by an inline message from a blacklisted source bot. Check that only the inline message disappears, the account cannot send more messages, and its earlier message remains.

Remove the account's restriction in the group's member settings, then send another test message or sticker. Report it first from an ordinary member account and check that it remains. Report it from an administrator account and check that the target and report disappear, the sender cannot comment through the linked channel, and they cannot rejoin through an invite link. Check separately whether Telegram removed their earlier messages. Other users' messages should remain.

Repeat the administrator report with **Remain Anonymous** enabled and send as the group itself. Also test a commenter who has not joined the discussion group. Check target deletion and both commenting and rejoining restrictions.

To check administrator protection, report a message from an administrator. Only that message should disappear; the administrator should still be able to send messages and retain their other history. Remove test restrictions or bans after each round. Use disposable accounts and groups for tests that delete history.

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
- For report storage failures, check the `REPORTS` binding, database migrations, and D1 usage in the Cloudflare dashboard.

`pending_update_count: 0` only means there is no backlog. Verify message and membership changes in the test group. Run `unset BOT_TOKEN` when finished.

## View report evidence

Open the `reports` table in the `anti-fwd-spam-reports` D1 database in the Cloudflare dashboard. `raw_update` contains Telegram's complete report JSON, including the replied-to message. `classification` contains content-type and media-field classifications. Keep member information private when viewing or exporting records.

Reports are retained for 3 days. The bot does not download images or other media. Scheduled cleanup removes expired records; service failures or exhausted quotas can delay deletion. Cloudflare backups have separate retention rules; see [D1 Time Travel](https://developers.cloudflare.com/d1/reference/time-travel/).
