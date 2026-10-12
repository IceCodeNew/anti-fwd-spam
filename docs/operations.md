# Operations

This page covers routine moderation, false-positive recovery, troubleshooting, and data handling after deployment. The stable routing and protection rules live in the [behavior contract](behavior.md); do not use this page as a second implementation specification.

## Reports and sources

Use an identity in `REPORTER_IDS` to reply to a spam message and mention the bot. The bot can remove the target, ban its sender, and clean eligible indexed history. An unlisted valid reply report records evidence without automatic punishment. A group owner or administrator remains protected from automatic bans, but a trusted report can still delete its explicit target.

Use `/bs @source_bot` to register a source bot. To handle both an inline sender and its source, reply to the inline message with bare `/bs`. Use the complete `https://t.me/addstickers/<set_name>` URL with `/bs` to register a sticker set without a source lookup. See [the behavior contract](behavior.md) for command forms, authorization, and retry outcomes.

## False positives and restrictions

Automatic filtering deletes the triggering message and can permanently mute a human sender. It does not add that sender to a blacklist. Administrators and group owners are protected. A confirmed ban or deletion may remove history; lifting a Telegram restriction does not restore deleted messages.

Inspect D1 in the Cloudflare dashboard. Replace the example IDs with the moderation bot ID and the value from your database:

```sql
SELECT source_id FROM blacklisted_sources ORDER BY source_id;
SELECT bot_id, user_id FROM blacklisted_users ORDER BY bot_id, user_id;
SELECT set_name, added_at FROM blacklisted_sticker_sets
WHERE bot_id = 123456789 ORDER BY set_name;
SELECT file_unique_id, added_at FROM blacklisted_stickers
WHERE bot_id = 123456789 ORDER BY file_unique_id;
```

Remove only the rule you intend to change:

```sql
DELETE FROM blacklisted_sources WHERE source_id = 987654321;
DELETE FROM blacklisted_users WHERE bot_id = 123456789 AND user_id = 987654321;
DELETE FROM blacklisted_sticker_sets
WHERE bot_id = 123456789 AND set_name = 'example_set';
DELETE FROM blacklisted_stickers
WHERE bot_id = 123456789 AND file_unique_id = 'example_unique_id';
```

Database removal affects future messages; lift an existing mute or ban separately in Telegram. Keep member information private when viewing or exporting D1 data.

## Delivery and troubleshooting

Check webhook delivery from a shell that holds `BOT_TOKEN`:

```bash
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

`result.url` should be the Worker URL ending in `/webhook`. A growing update backlog or a new `last_error_message` requires checking Worker logs. To intercept unsolicited messages from other bots, including guest bots, the receiving moderation bot must have Bot-to-Bot Communication Mode enabled and satisfy Telegram's group-delivery conditions (administrator rights or disabled Group Privacy Mode). Seeing a bot message nested in a reply report does not prove its original update was delivered. Enablement is described in [setup](setup.md#telegram-bot-and-permissions).

| Symptom | Check |
| --- | --- |
| Reports or `/bs` do nothing | Check the sending identity, `REPORTER_IDS`, and `BOT_USERNAME`. |
| Webhook returns `401` | Confirm the saved and registered `TELEGRAM_WEBHOOK_SECRET` match. |
| Webhook returns `404` | Use the deployed URL ending in `/webhook`. |
| Deletion, mute, or ban fails | Check bot administrator permissions and target protection. |
| `ban confirmation failed` or `mute retry skipped` | Check membership manually before retrying a report. |
| History cleanup is incomplete | Telegram and the index only cover messages the bot received within the retention window. |
| Storage errors | Check the `REPORTS` binding, migrations, and D1 limits. |

## Privacy and retention

The bot sends bounded message context and selected profile data to Jev only when Jev is enabled. It does not download or inspect media. Review provider terms before enabling it.

D1 keeps reports, temporary processing records, and moderation progress for a limited retention period. The recent-message index stores identifiers and timestamps rather than message text; pending model tasks may temporarily retain submitted content until classification finishes. Source, account, and sticker blacklists remain until removed. Exact retention constants are maintained in `src/anti_fwd_spam/evidence.py` and cleanup is performed by the scheduled Worker trigger.

Keep tokens, secrets, member IDs, report JSON, and database exports out of public logs and repository files.
