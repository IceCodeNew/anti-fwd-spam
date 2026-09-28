# Message processing and reporting

This document defines the bot's functional contract: accepted inputs, authorization, routing, and moderation outcomes. Changes to these behaviors must be intentional and reflected here and in behavior tests. For deployment and everyday commands, use the [README](../README.md).

## Inputs and reporting authorization

Telegram sends updates to `POST /webhook`. The Worker verifies `TELEGRAM_WEBHOOK_SECRET` before parsing the update. `parse_update` in [policy.py](../src/anti_fwd_spam/policy.py) then validates every field that routing uses, one time, and returns a `TelegramUpdate`. An update with a malformed message gets HTTP 400 and no moderation. The scheduled trigger is a separate entrypoint for cleanup and model retries.

```diagram
┌──────────────────────────────┐
│ Authenticated Telegram update│
└──────────────┬───────────────┘
               │
               ├── /bs in private or group chat ──▶ Command plugins ──┐
               │                                                      │
               └── Group message ──▶ Blacklist checks                 │
                                           │ no match                 │
                                           ▼                          │
                                     Index message                    │
                                           │                          ▼
                                           ├── Reply + bot mention ──▶ REPORTER_IDS
                                           │   (reply plugins)        │
                                           │                          ├── Unlisted /bs @name: ordinary filtering
                                           │                          ├── Unlisted reply: save evidence
                                           │                          │
                                           │                          └── Allowed
                                           │                              │
                                           │              ┌───────────────┴──────────────┐
                                           │              ▼                              ▼
                                           │        /bs handler                    Reply handler
                                           │        Save source ID                 Record evidence
                                           │        Bare reply: register source      Check target protection
                                           │          + action 2 on A and B         Apply action 2 if allowed
                                           │        Reply with ID
                                           │        Clean group command
                                           │
                                           └── No report ──▶ Local rules ──▶ Jev
```

Commands are checked before automatic moderation. A listed command ends routing. This also applies to an edited `/bs`, which the handler ignores. An unlisted bare `/bs` replying to a valid group target saves evidence without registering a source or punishing either account. An unlisted `/bs @username` does not register a source or save report evidence; it continues through ordinary message filtering. Reply reports are checked after blacklist handling and indexing. A valid unlisted reply mentioning the bot saves evidence and ends routing without automatic moderation. Private messages other than listed commands are ignored.

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

For a direct message without a reference, the webhook regex searches the current text, caption, shared contact card name, and sender nickname. A miss queues a D1 task. In the background, the Worker fetches the sender's private profile and, when a contact card has a usable `user_id`, the contact account's private profile. The deferred regex checks the returned sender biography and contact account nickname and biography. Jev then evaluates the available complete input if regex did not match. A missing contact `user_id` or failed lookup leaves its account profile unknown; the card name remains available. The contact phone number is not sent to Jev.

A message with an explicit same-chat reply, external reply, or quote bypasses both local regex stages. Jev sees the current message body, sender nickname and biography as one unit and the available referenced body, sender nickname and biography as another. It estimates the probability that the reference is advertising **and** the current sender intends to promote it. Without a reference, Jev estimates whether the current unit is advertising; for a contact card, it estimates whether the card name and available contact account profile advertise spam. Same-chat replies include the referenced message body and user nickname; a referenced user ID permits a biography lookup. External replies may supply only quoted text and origin information, so unavailable body, nickname, or biography is omitted. Only the current sender can receive Action 1. Without model keys, quoted messages cannot be classified by regex. Exact patterns and model settings belong to `SPAM_PATTERNS` in [policy.py](../src/anti_fwd_spam/policy.py) and `SPAM_THRESHOLD` / `MODEL_PROVIDERS` in [model.py](../src/anti_fwd_spam/model.py).

### Action 1: delete the current message and permanently mute

`Actions.delete_and_mute` deletes the triggering message and permanently mutes a human sender in a supergroup. It preserves earlier messages and does not add either identity to a blacklist. Bots are not muted. Owners, administrators, and messages sent as the destination group's anonymous identity are protected before deletion.

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
| Listed reply report | Apply Action 2 to the actual sender A. A's use of B does not authorize any action against B. Confirmed bans add A to `blacklisted_users`. |
| Unlisted reply report or bare `/bs` reply | Save the update as evidence without registering a source or punishing either account. |
| Authorized reply with bare `/bs` | Read B from the target's `via_bot.id`, add B to `blacklisted_sources`, and apply Action 2 separately to A and B. Confirmed bans add each account to `blacklisted_users`. No username lookup is needed. |
| Message from listed source B | Apply Action 1 to A and Action 2 to B. A is not added to either blacklist by this source match. |
| Message from an account in `blacklisted_users` | Apply Action 2 to that account in the receiving supergroup. |
| Regex or Jev spam match | Apply Action 1 to the current sender; neither blacklist changes. |

`blacklisted_sources` is a source-ID set. `blacklisted_users` is scoped by moderation bot ID. A source can also be present in the account blacklist after a confirmed ban. Temporary evidence, indexed messages, and operation progress have separate retention; see [Data storage](../README.md#data-storage).

### Inline messages: choose which account to report

For a message sent by A through bot B, automatic source matching establishes a blocked source but does not establish A's intent. An ordinary reply report targets A alone. Replying with `/bs` explicitly reports both accounts.

```diagram
Listed source B ──▶ Action 1 on A + Action 2 on B
Reply + mention ──▶ Action 2 on A only
Reply + /bs     ──▶ Register B + Action 2 on A + Action 2 on B
```

The bare reply command works in groups and accepts `/bs@moderation_bot` too. In a private chat, a listed bare `/bs` reply gets the usage reply. For listed senders, missing or invalid `via_bot` produces a usage reply without adding a source or punishing either account. Explicit `/bs @username` registers the named source only for listed identities. An unlisted bare reply saves evidence only. Listed combined reports can punish A and B regardless of the reporter's group role. Administrator protection and retry progress apply separately to A and B.

Moderation runs before acknowledgement, so a rejected reply cannot prevent punishment. Retryable failures leave command cleanup pending. If a ban outcome cannot be confirmed, the bot retains the command and asks the reporter to check membership before submitting a new report; it does not blindly repeat the ban. Otherwise, it removes the group command after processing, even if Telegram permanently rejects the acknowledgement.

## Edits and retries

Edited group messages cancel pending model work within the message window. They still undergo source filtering and reply-report handling. Edited messages without a reference also undergo local regex checks on text, captions, contact names, and nicknames; a match applies Action 1. Edited messages with a reference bypass local regex and do not start fresh Jev classification. Edits skip automatic account-blacklist checks and profile lookups. Edited `/bs` commands from authorized reporters are ignored.

Model classification starts with the first configured entry in `MODEL_PROVIDERS`. Failed requests can rotate through the configured providers, including CommandCode's System One endpoint. The classification budget is one initial attempt and three retries, delayed by 1, 2, and 5 minutes. With five configured providers, the fifth is outside that budget. A valid non-spam score or an invalid answer stops classification without trying another provider.

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
