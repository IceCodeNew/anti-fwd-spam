# Message processing and reporting

This document defines the bot's functional contract: accepted inputs, authorization, routing, and moderation outcomes. Changes to these behaviors must be intentional and reflected here and in behavior tests. For deployment and everyday commands, use the [README](../README.md).

## Inputs and reporting authorization

Telegram sends updates to `POST /webhook`. The Worker verifies `TELEGRAM_WEBHOOK_SECRET` before it parses the update. `parse_update` in [policy.py](../src/anti_fwd_spam/policy.py) validates message structure, identities, and account provenance, then returns a `TelegramUpdate`. An update with a malformed message gets HTTP 400 and no moderation. Optional sticker metadata with no usable set name does not match the set list; it does not invalidate the update. The scheduled trigger is a separate entrypoint for cleanup and model retries.

```diagram
┌──────────────────────────────┐
│ Authenticated Telegram update│
└──────────────┬───────────────┘
               │
               ├── Listed /bs in any chat ────────▶ Command plugins ──┐
               │                                                      │
               └── Group message ──▶ Blacklist checks                 │
                                           │ no match                 │
                                           ▼                          │
                                     Index message                    │
                                           │                          ▼
                                     ├── Reply + bot mention ──▶ REPORTER_IDS
                                     ├── Unlisted bare /bs ────▶ Evidence only
                                           │   (reply plugins)        │
                                           │                          ├── Unlisted /bs with argument: ordinary filtering
                                           │                          ├── Unlisted reply: save evidence
                                           │                          │
                                           │                          └── Allowed
                                           │                              │
                                           │              ┌───────────────┴──────────────┐
                                           │              ▼                              ▼
                                           │        /bs handler                    Reply handler
                                           │        Register source / sticker      Record evidence
                                           │        Bare reply: Action 2 on A       Check target protection
                                           │          + B when via_bot is present  Apply action 2 if allowed
                                           │        Acknowledge result
                                           │        Clean group command
                                           │
                                           └── No report ──▶ Local rules ──▶ Jev
```

Listed commands are checked before automatic moderation. A listed command ends routing. An edited listed `/bs` is ignored as a command and fences any earlier model task. An unlisted bare `/bs` is checked against blacklists first; when it replies to a valid group target without a blacklist match, it saves evidence without registering a source or punishing either account. An unlisted `/bs` with an explicit username or sticker-set URL does not register a source or save report evidence; it continues through ordinary message filtering. Reply reports are checked after blacklist handling and indexing. A valid unlisted reply mentioning the bot saves evidence and ends routing without automatic moderation. Private messages other than listed commands are ignored.

In a forum topic, Telegram attaches the topic's creation message to messages that do not reply to anything. That creation message is never a report target, so a mention or bare `/bs` in a topic reports only when it replies to another message.

`Reporting.dispatch` in [reporting.py](../src/anti_fwd_spam/reporting.py) owns authorization for both plugin groups:

- With `sender_chat`, use that sending identity alone. A listed compatibility `from.id` cannot authorize an unlisted group or channel.
- Otherwise, use a genuine non-bot `from.id`. Match the numeric ID against `REPORTER_IDS` on every delivery, including retries.

An empty allowlist permits no direct report moderation or source registration. Any identity can submit a valid group reply report as evidence. A listed identity can trigger Action 2 for a reply report regardless of its group administrator status. A group owner or administrator outside the allowlist can save evidence but cannot trigger Action 2. Target administrators remain protected from bans.

## Blacklists and content checks connect to two actions

The following map describes new supergroup messages after command handling. A is the actual sender; B is a listed source bot. Account and source matches can both apply to one message.

```diagram
┌─────────────────────────────┐      ┌───────────────────────────────┐
│ New supergroup message      │─────▶│ Sender in blacklisted_users?  │── yes ──▶ Action 2 on A
└──────────────┬──────────────┘      └───────────────────────────────┘
               │
               │                    ┌───────────────────────────────┐
               ├───────────────────▶│ Source in blacklisted_sources?│── yes ──┬─▶ Action 1 on A *
               │                    └───────────────────────────────┘         └─▶ Action 2 on B
               │
               │ neither blacklist matches
               ▼
       Index eligible message
               │
               ├── Reply report ──▶ REPORTER_IDS match ──▶ Action 2 on reported sender
               │                   unlisted ──▶ Save evidence only
               │
               │ no report
               ▼
       No reference: regex on authored text/card name/nickname ── match ─▶ Action 1 on A
               │ no match
               ▼
       Current sticker set or individual sticker listed? ── yes ───────▶ Action 1 on A
               │ no match
               ▼
       Background: fetch available user profiles
       No reference: regex on sender/contact profile ── match ────────▶ Action 1 on A
               │ no match
               ▼
       Jev: sender, reference, contact units ── spam ────────────────▶ Action 1 on A
               │
               └── Below threshold / no model configured ──▶ Leave message unchanged

* If A already matches the account blacklist, reuse A's Action 2 result.
```

Source matching uses `via_bot.id` and visible bot origins in `forward_origin.sender_user`. It does not inspect copied text or hidden forwarding origins. All matched sources are handled independently. See `parse_update` in [policy.py](../src/anti_fwd_spam/policy.py).

For a group message without a reference, the webhook regex searches the current text, caption, shared contact card name, and sender nickname. After a regex miss, the bot checks the current sticker set, then the individual sticker; a match applies Action 1 without a model. A miss queues a D1 task. In the background, the Worker fetches the sender's private profile and, when a contact card has a usable `user_id`, the contact account's private profile. The deferred regex checks the returned sender biography and contact account nickname and biography. Jev then evaluates the available complete input if regex did not match. A missing contact `user_id` or failed lookup leaves its account profile unknown; the card name remains available. The contact phone number is not sent to Jev.

A message with an explicit same-chat reply, external reply, or quote bypasses regex and sticker-list checks. A forum topic creation attachment is not a reference for this purpose. Jev sees the current message body, sender nickname and biography as one unit and the available referenced body, sender nickname and biography as another. It estimates whether the current sender independently advertises spam, **or** the reference advertises spam **and** the current sender intends to promote it. An ordinary reference does not exempt the current sender's own advertisement. Warnings, objections, and reports do not count as promotion; an invite alone does not establish spam. Without a reference, Jev estimates whether the current unit is advertising; for a contact card, it estimates whether the card name and available contact account profile advertise spam. Same-chat replies include the referenced message body and user nickname; a referenced user ID permits a biography lookup. External replies may supply only quoted text and origin information, so unavailable body, nickname, or biography is omitted. Only the current sender can receive Action 1. Without model keys, content checks cannot classify a referenced message. Exact patterns and model settings belong to `SPAM_PATTERNS` in [policy.py](../src/anti_fwd_spam/policy.py) and `SPAM_THRESHOLD` / `MODEL_PROVIDERS` in [model.py](../src/anti_fwd_spam/model.py).

Jev treats product and service offers from a supplier or promoter to group readers as spam advertising. A short sales pitch can list features, coverage, after-sales service, or warranty without a price, contact, link, or order instruction. Reply-topic relevance provides context but does not exempt a sales pitch. Off-topic speech alone is insufficient. Price questions, factual feature answers, personal use or transaction accounts, non-soliciting advice in response to a request, personal order support, warnings, and reports are exempt from this product-offer rule. A description of a third-party service's payment terms alone does not establish solicitation. Product names, features, and warranty terms alone are insufficient. These exemptions do not override the specific advertising rules below. These checks require an eligible Jev check and have no dedicated local regex rule.

Jev treats off-topic private-group recruitment, sexual offers, payment-code recruitment, and exaggerated daily-income recruitment as spam advertising. Payment-code recruitment does not require an explicit income promise. A contact card that promises thousands per day or a five-digit daily income can advertise recruitment without a separate link. Jev interprets slang, homophones, misspellings, and mixed letters and digits in context. Sexual offers can combine body descriptions with contact or payment terms without explicit sexual words. Texture or payment terms alone do not establish a sexual offer. Generic curiosity bait with an unrelated private-group invite can meet the spam condition without proof of fraud. Topic-related invitations, merchant payment questions, ordinary job or income discussion, warnings, and reports do not meet this condition. These recruitment checks require an eligible Jev check; they have no dedicated local regex rule.

Jev also treats cryptocurrency solicitation as spam advertising. This includes offers to buy, sell, or exchange coins; investment and trading signals; mining and airdrop offers; wallet and transfer services; and agent or referral recruitment. The rule covers common cryptocurrencies, such as BTC, ETH, SOL, BNB, XRP, DOGE, USDT, and USDC, as well as TRON/TRX energy rental. Prices, return promises, orders, claims, or contact instructions can establish solicitation even if the service is free or legal. Coin names, energy terms, bot handles, or links alone are insufficient. Technical discussion, market analysis, price questions, personal order support, warnings, and reports do not count as solicitation. These checks require an eligible Jev check; they have no dedicated local regex rule.

### Action 1: delete the current message and permanently mute

`Actions.delete_and_mute` deletes the triggering message and permanently mutes a human sender in a supergroup. It preserves earlier messages and does not change any blacklist. Bots are not muted. Owners, administrators, and messages sent as the destination group's anonymous identity are protected before deletion.

Local rules, Jev, and source filtering share this action. Ordinary groups support deletion but not permanent muting.

### Action 2: ban and clean indexed history

`Actions.delete_history_and_ban` bans the account, prevents rejoining, and removes its eligible indexed messages in the affected group. Automatic matches include the triggering message only if it belongs to that account and was indexed. A reply report also supplies an explicit target message to delete. The report deletes that target even when Telegram permanently rejects the ban; only a ban that waits for a retry postpones the deletion. Source B's cleanup therefore does not select A's message as B's own history.

Confirmed bans add the target to `blacklisted_users`. Owners and administrators cannot be banned; an authorized reply report can still delete the explicitly reported administrator message. History selection and Telegram's own deletion behavior are described under [Administrator protection and history limits](../README.md#administrator-protection-and-history-limits).

Reply reports in supergroups apply the same ban and indexed-history cleanup to human and bot accounts. Group owners and administrators remain protected from bans and history cleanup. Reports targeting a sender-chat identity, and reports in basic groups, can delete the explicit target but do not select a user account to ban. Automatic account-blacklist checks also run only on new supergroup messages.

Both actions in [actions.py](../src/anti_fwd_spam/actions.py) own membership checks, duplicate-operation handling, and retry outcomes. They never unban to repeat a ban. Durable progress prevents a redelivered update from blindly repeating restrictions after a manual unban or unmute; uncertain outcomes can require manual inspection.

## What is stored

| Input or result | Persistent effect |
| --- | --- |
| Authorized `/bs @example_bot` | Resolve B and add B to `blacklisted_sources`; acknowledge the ID. No immediate ban or history cleanup. |
| Authorized `/bs https://t.me/addstickers/<set_name>` | Add the whole set to `blacklisted_sticker_sets`. No immediate moderation, even when the command replies to a message. |
| Listed reply mentioning the bot | Apply Action 2 to the actual sender A, human or bot. Do not register a sticker set or source B, or punish B. Confirmed bans add A to `blacklisted_users`. |
| Unlisted reply report or bare `/bs` reply | Save the update as evidence without registering a source or punishing either account. |
| Authorized reply with bare `/bs` to a sticker | Register the whole set from the target's `sticker.set_name` and apply Action 2 to A. If valid `via_bot` is also present, register B and apply Action 2 to B too. |
| Authorized reply with bare `/bs` to an inline bot message | Read B from the target's `via_bot.id`, add B to `blacklisted_sources`, and apply Action 2 separately to A and B. Confirmed bans add each account to `blacklisted_users`. No username lookup is needed. |
| Message from listed source B | Apply Action 1 to A and Action 2 to B. A is not added to any blacklist by this source match. |
| Message from an account in `blacklisted_users` | Apply Action 2 to that account in the receiving supergroup. |
| Authorized bare `/bs` reply to a sticker without a usable set name | Register its usable `file_unique_id` in `blacklisted_stickers` and apply Action 2 to A. A valid `via_bot` also registers and receives Action 2. |
| No-reference message from a listed sticker set or individual sticker | Apply Action 1 to the current sender; no history cleanup or blacklist changes. |
| Regex or Jev spam match | Apply Action 1 to the current sender; no blacklist changes. |

`blacklisted_sources` is a source-ID set. `blacklisted_users` is scoped by moderation bot ID. A source can also be present in the account blacklist after a confirmed ban. `blacklisted_sticker_sets(bot_id, set_name, added_at)` stores whole sets with primary key `(bot_id, set_name)`. Commands and message metadata use the same ASCII set-name validation and lowercase normalization; only an exact full-name match counts. Parameter-bound, indexed lookups query one set for the current moderation bot, without loading or caching the whole list. Registration is idempotent and preserves the first `added_at`. Removing an entry affects subsequent messages immediately; permanent lists do not expire with temporary evidence. Temporary evidence, indexed messages, and operation progress have separate retention; see [Data storage](../README.md#data-storage).

`blacklisted_stickers(bot_id, file_unique_id, added_at)` stores individual stickers with primary key `(bot_id, file_unique_id)`. IDs remain case-sensitive and must contain 1–128 ASCII letters, digits, underscores, or hyphens. Registration prefers the whole set; only a missing or unusable set name permits individual registration. Individual matching applies even if a later message supplies a set name. Registration is idempotent, preserves the first timestamp, and does not expire with reports. Parameter-bound lookups use the composite key without a list cache.

Sticker-list checks retain the message-age and reference gates and inspect only the current sticker, not a referenced sticker. A regex hit needs no sticker-list lookup. An unusable set name or unique ID causes no lookup for that list. A set hit skips the individual lookup. A failed lookup returns HTTP 503 for unavailable sticker-list storage, not a no-match result. Download file IDs and thumbnail IDs never identify blocked stickers. Commands do not fetch the URL, call `getStickerSet`, download images, or infer a source bot from a set-name suffix. Migrations contain no real malicious-set or bot-ID seed; authorized operators register stickers after deployment.

### Inline messages: choose which account to report

For a message sent by A through bot B, automatic source matching establishes a blocked source but does not establish A's intent. An ordinary reply report targets A alone. Replying with `/bs` explicitly reports both accounts.

```diagram
Listed source B ──▶ Action 1 on A + Action 2 on B
Reply + mention ──▶ Action 2 on A only
Reply + /bs     ──▶ Register B + Action 2 on A + Action 2 on B
```

The bare reply command works in groups and accepts `/bs@moderation_bot` too. `reported_target` validates the group-local reply target before registering any source or moderating: cross-chat, malformed, and forum topic-root targets cannot trigger those effects. In a private chat, a listed bare `/bs` reply gets the usage reply. For a sticker, `/bs` registers the whole set and applies Action 2 to A; a valid `via_bot` also registers B and applies Action 2 to B. A and B are handled once if they are the same account. An absent or unusable set name permits individual registration through a usable `sticker.file_unique_id`. A valid `via_bot` is registered separately. Download file IDs and thumbnail IDs are not substitutes. If none is usable, the bot gives usage help without registration or punishment; a reply mentioning the bot can report A alone. An unlisted bare reply saves evidence only. Listed combined reports can punish A and B regardless of the reporter's group role. Administrator protection and retry progress apply separately to A and B.

Explicit `/bs @username` and `/bs https://t.me/addstickers/<set_name>` register only the specified source or whole set for listed identities, even when sent as replies. Sticker URLs must use that complete HTTPS form: lookalike hosts, extra paths, query strings, fragments, and `@https://…` are invalid. An invalid explicit argument never falls back to bare-reply reporting.

The bot saves command evidence before source registration. Each required registration must succeed before moderation or success acknowledgement. A partial registration followed by a storage failure returns HTTP 503; idempotent retries finish the remaining work. Protected targets and permanent moderation refusals do not remove registered sources. Moderation occurs before acknowledgement, so a rejected reply cannot prevent punishment.

Mention reports remove the report through Action 2. Bare `/bs` commands retain the command until acknowledgement and cleanup, including sticker-only reports. Retryable storage, moderation, acknowledgement, or cleanup failures return HTTP 503 and leave the command pending. Retries reuse per-account progress and do not repeat a confirmed ban after a manual unban; acknowledgement can repeat.

If the bot cannot confirm a ban outcome, it retains the command and asks the reporter to check membership before a new report. It does not repeat the unconfirmed ban. Otherwise, it removes the group command after it completes the operation, even if Telegram permanently rejects the acknowledgement. Private-chat commands remain.

## Edits and retries

Edited group messages fence earlier model work within the message window. They still undergo source filtering and reply-report handling unless a listed `/bs` command ends routing. Edited messages without a reference undergo local regex checks on text, captions, contact names, and nicknames, followed by the current sticker-list check after a regex miss; a match applies Action 1. Referenced edits bypass regex and sticker-list checks and start a fresh Jev classification. Edits skip automatic account-blacklist checks; only referenced edits fetch profiles for Jev. Edited `/bs` commands from authorized reporters are ignored as commands but still fence earlier model work.

Model classification starts with the first configured entry in `MODEL_PROVIDERS`. Failed requests can rotate through the configured providers, including CommandCode's System One endpoint. The classification budget is one initial attempt and three retries, delayed by 1, 2, and 5 minutes. With five configured providers, the fifth is outside that budget. A score at or above `SPAM_THRESHOLD` triggers Action 1. A lower score or an invalid answer stops classification without trying another provider.

```diagram
┌───────────────────────────┐
│ Retryable webhook failure │──▶ HTTP 503 ──▶ Telegram redelivery
└───────────────────────────┘                      │
                                                   └─▶ Same routing + reporter authorization
┌───────────────────────────┐
│ Pending D1 model task     │◀── Retryable classification or moderation failure
└─────────────┬─────────────┘
              │ due
              ▼
┌───────────────────────────┐
│ Scheduled trigger         │──▶ Resume due tasks ──▶ Biography, Jev, or pending Action 1
│ Expire temporary records  │
└───────────────────────────┘
```

Webhook and scheduled retries are distinct. The first task attempt runs in the background after the webhook response. Each scheduled run claims and completes due tasks one at a time, at most `SCHEDULED_TASKS_PER_RUN` per run. A retry reads the biography again before it asks Jev. Without model keys, the scheduled trigger still completes pending deletions and unstarted biography checks, but model retries pause until a key is configured. Model failures do not count as spam. See `ModelTasks.run` in [tasks.py](../src/anti_fwd_spam/tasks.py) for task progress and `Default.scheduled` in [entry.py](../src/entry.py) for the scheduled entrypoint.

## Add a reporting entrypoint

A reporting plugin is an in-process `ReportingPlugin` with a diagnostic name, a side-effect-free `matches(update)` function, an async `handle(update)` function for listed identities, and optionally an async `record_denied(update)` function for unlisted evidence. The functions receive the validated `TelegramUpdate`, which also holds the message and the raw JSON. `Reporting.dispatch` runs only the first matching plugin. It checks authorization before invoking either handler and maps storage and Telegram errors to retry responses.

Register the plugin in `Moderator.command_plugins` for commands handled before filtering, or `Moderator.reply_plugins` for group reports handled after blacklist checks. Both collections are bound in `Moderator.__init__` in [moderation.py](../src/anti_fwd_spam/moderation.py). Matchers must not resolve usernames, write records, or moderate messages. Those operations belong in the authorized handler. Call handlers through the dispatcher, never from a separate route.

The handler validates its target and delegates punishment to the shared actions. A `REPORTER_IDS` match authorizes direct report moderation, subject to target administrator protection. Plugins are trusted repository code, not sandboxed third-party modules.

For a new entrypoint, cover authorized and denied identities, sender-chat impersonation, and authorization revoked before retry. [test_reporting.py](../tests/test_reporting.py) exercises an additional plugin through the shared gate; [report-authorization.test.mjs](../tests/report-authorization.test.mjs) and [sources.test.mjs](../tests/sources.test.mjs) exercise the shipped entrypoints through the Worker and isolated D1.
