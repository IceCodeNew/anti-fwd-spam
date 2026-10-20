# Operations

This page covers routine moderation, false-positive recovery, troubleshooting, and data handling after deployment. The stable routing and protection rules live in the [behavior contract](behavior.md); do not use this page as a second implementation specification.

## Reports and sources

Use an identity in `REPORTER_IDS` to reply to a spam message and mention the bot. The bot can remove the target, ban its sender, and clean eligible indexed history. An unlisted valid reply report records evidence without automatic punishment. A group owner or administrator remains protected from automatic bans, but a trusted report can still delete its explicit target.

Use `/bs @source_bot` to register a source bot, including a guest bot that sends messages directly. A named command registers the source only, even when sent as a reply. To register and immediately report a direct bot sender, reply to its message with bare `/bs`; no username lookup is needed. Bare `/bs` on an inline message still handles both its sender and inline source. Use the complete `https://t.me/addstickers/<set_name>` URL with `/bs` to register a sticker set. See [the behavior contract](behavior.md) for authorization and retry outcomes.

## False positives and restrictions

Ordinary message filtering deletes the triggering message and can permanently mute a human sender without adding an account blacklist entry. Join-profile filtering instead permanently bans the matched new member and deletes the join notice, recording the confirmed account ban. Administrators and group owners are protected. Deleted messages cannot be restored.

To undo an account ban or permanent mute, use an identity in `REPORTER_IDS` to send `/allow @username` or `/allow <account ID>` in the affected supergroup. Known usernames are retained on confirmed account bans and checked against that account's current numeric profile. Prefer the numeric ID for legacy entries without a name, renamed or ambiguous accounts, or when Telegram cannot resolve the identity. The command removes this bot's account entry and the matching shared source entry, unbans a kicked account or lifts a restricted member's personal restrictions in the current group. It does not automatically add a removed account back, change group-wide default permissions, or affect other groups' bans/restrictions, other bots' account entries, or sticker lists. This is not a lasting exemption from future advertisements or new reports.

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

Manual database removal affects future filtering; lift its existing Telegram restriction separately. `/allow` handles account-ban and mute recovery together with list removal; confirmed recovery is not repeated during cleanup retries. If the command reports uncertain recovery, inspect membership/permissions before sending a new command; the old request will not blindly repeat a permission-changing operation. Keep member information private when viewing or exporting D1 data.

## Delivery and troubleshooting

Check webhook delivery from a shell that holds `BOT_TOKEN`:

```bash
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

`result.url` should be the Worker URL ending in `/webhook`. A growing update backlog or a new `last_error_message` requires checking Worker logs. To intercept unsolicited messages from other bots, including guest bots, the receiving moderation bot must have Bot-to-Bot Communication Mode enabled and satisfy Telegram's group-delivery conditions (administrator rights or disabled Group Privacy Mode). Seeing a bot message nested in a reply report does not prove its original update was delivered. Enablement is described in [setup](setup.md#telegram-bot-and-permissions).

| Symptom | Check |
| --- | --- |
| Reports, `/bs`, or `/allow` do nothing | Check the sending identity, `REPORTER_IDS`, and `BOT_USERNAME`. |
| Webhook returns `401` | Confirm the saved and registered `TELEGRAM_WEBHOOK_SECRET` match. |
| Webhook returns `404` | Use the deployed URL ending in `/webhook`. |
| Deletion, mute, or ban fails | Check bot administrator permissions and target protection. |
| `ban confirmation failed` or `mute retry skipped` | Check membership manually before retrying a report. |
| History cleanup is incomplete | Telegram and the index only cover messages the bot received within the retention window. |
| Model classification does not run | Check for a supported Luna provider key in [setup](setup.md#optional-model-provider). Deployments with only retired Jev-provider keys need [migration](setup.md#compatibility-and-migration). |
| A different model runs than expected | Supported keys select Luna. Check the deployed code and [compatibility and migration](setup.md#compatibility-and-migration) when upgrading an older deployment. |
| Model returns an error or refusal | Inspect redacted Worker logs and provider access/quota. Connection/HTTP failover stays within the fixed shared attempt budget. A completed invalid or refused answer ends classification without punishment or another provider call. Use [live model checks](jev-checks.md) with approved fixtures to test semantic behavior. |
| Model usage incurs unexpected charges | Check provider billing and limits. Experiential's free allowance is not unlimited; its exhaustion can advance to configured paid providers. The provider waterfall is not a strict-free mode. See [setup](setup.md#failover-and-costs). |
| Storage errors | Check the `REPORTS` binding, migrations, and D1 limits. |

## Privacy and retention

The bot sends bounded text context and selected profile data to a configured decision-model provider only when classification is enabled. It does not download or inspect images, video, or audio. Provider image support does not change this boundary; see [model providers and media](model-providers.md). Review provider terms before enabling classification.

D1 keeps reports, temporary processing records, and moderation progress for a limited retention period. The recent-message index stores identifiers and timestamps rather than message text; pending model tasks may temporarily retain submitted content until classification finishes. Source, account, and sticker blacklists remain until removed. Member-activity counters persist after their observations expire, capped at the maturity threshold; changing the message index window changes how quickly that threshold can be earned. Exact retention constants are maintained in `src/anti_fwd_spam/evidence.py` and cleanup is performed by the scheduled Worker trigger.

Keep tokens, secrets, member IDs, report JSON, and database exports out of public logs and repository files.
