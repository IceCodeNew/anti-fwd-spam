# Message processing and reporting

This is the bot's stable behavior contract. It describes what Telegram users and trusted operators can expect from an authenticated update. Deployment steps belong in [setup](setup.md); routine operations belong in [operations](operations.md); development and extension guidance belongs in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Scope and authorization

The Worker accepts authenticated Telegram `message` and `edited_message` updates at `POST /webhook`. Invalid content is rejected without moderation. Private messages are ignored except for supported commands.

A reporting identity is either a configured user ID or a configured sender-chat ID. A sender-chat identity is evaluated on its own; a compatibility `from` field cannot authorize it. An ordinary user must be a genuine non-bot sender whose ID is configured. Authorization is checked again on redelivery and retry.

- A listed `/bs` command is handled before automatic filtering.
- An unlisted bare `/bs` reply to a valid group target records evidence but does not register a source or punish an account.
- An unlisted reply that mentions the bot records evidence and ends routing.
- An explicit unlisted `/bs` argument continues through ordinary filtering instead of registering a source.
- A report target must be a valid message in the same group; a forum topic's creation message is not a target.

## Routing order

For a new group message, processing follows this order:

1. Validate the update and authenticated webhook request.
2. Match explicit source identities and account blacklists.
3. Dispatch authorized commands and report plugins.
4. Index an eligible message for possible later history cleanup.
5. Apply reply-report handling.
6. Apply local text, profile, sticker, and maintenance rules where the message has no reference.
7. Queue Jev classification for the remaining eligible input.
8. Leave the message unchanged when no rule or model decision reaches the configured threshold.

```mermaid
flowchart TD
    A[Authenticated Telegram update] --> B{Supported command?}
    B -->|Listed /bs| C[Authorize and handle command]
    B -->|No| D{Group message?}
    D -->|No| E[Ignore, except commands]
    D -->|Yes| F[Match source and account blacklists]
    F -->|Source match| G[Action 1 on sender and Action 2 on source]
    F -->|Account match| H[Action 2 on account]
    F -->|No blacklist match| I[Index eligible message]
    I --> J{Reply report?}
    J -->|Trusted| K[Action 2 on reported sender]
    J -->|Unlisted| L[Save evidence only]
    J -->|No report| M{Reference present?}
    M -->|Yes| N[Jev with bounded reference context]
    M -->|No| O[Local text/profile/sticker rules]
    O -->|Match| P[Action 1 on current sender]
    O -->|No match| N
    N -->|Spam at threshold| P
    N -->|Below threshold or unavailable| Q[Leave message unchanged]
```

A message with a same-chat reply, external reply, or quote bypasses local regex and sticker-list checks. Jev receives only the bounded fields Telegram supplied for the current message and its reference. A forum topic attachment that is not an explicit reply does not count as a reference.

## Moderation decision map

The two moderation actions have different targets. A source match handles the sender and source independently; a trusted report handles the explicitly reported account; an automatic content match handles only the current sender.

```mermaid
flowchart LR
    A[Moderation trigger] --> B{Trigger type}
    B -->|Registered source| C[Current sender A: Action 1]
    B -->|Registered source| D[Source bot B: Action 2]
    B -->|Trusted reply report| E[Reported account A: Action 2]
    B -->|Local rule, sticker, or Jev| F[Current sender A: Action 1]
    G[Unlisted report] --> H[Evidence only]
    C --> I{Protected administrator or owner?}
    F --> I
    I -->|Yes| J[Keep message/account protected]
    I -->|No| K[Delete current message and mute when supported]
    D --> L[Ban and clean eligible indexed history]
    E --> L
```

## Moderation outcomes

### Action 1: remove the current message and mute its sender

Local rules, sticker matches, and Jev spam matches use Action 1. The bot removes only the triggering message and permanently restricts a human sender when Telegram supports that operation. It does not clean history or add a blacklist entry. Bots are not muted. Basic groups support deletion but not permanent mute.

Group owners, administrators, and messages sent as a group's anonymous identity are protected from automatic Action 1. A valid trusted report may still delete an explicitly reported administrator message.

### Action 2: ban an account and clean eligible history

Trusted reply reports and explicit source reports use Action 2. The bot bans the selected account when allowed, prevents rejoining, and removes eligible indexed messages in that group. A reported target message is deleted independently of the ban result when Telegram permits it. Confirmed bans add the account to the bot-scoped account blacklist.

Action 2 never treats the source bot as the sender's history unless the command explicitly reports both accounts. Target administrators remain protected from automatic bans and history cleanup.

## Local filtering

Messages without a reference are checked before Jev. General local patterns inspect authored text, captions, shared contact display names, and sender nicknames; deferred profile checks inspect the available sender and contact-account biographies after Telegram lookups. A separate maintenance pattern checks only current text and captions for a same-sentence claim that a group or channel is disabled, moved, or replaced followed by a request to join a new destination. Administrators are protected before Action 1, so ordinary administrator notices remain intact.

A local match ends model classification for that message. A reference bypasses all local regex and sticker-list checks, including on edits.

## Jev classification

Jev is optional. When enabled, the bot sends the current sender's bounded message context, selected profile data, contact display data, and bounded reference context to the configured provider. It does not download or inspect media. Provider failures are retried according to the durable task policy; invalid or unavailable model answers do not count as spam.

The current sender's intent is the primary evidence. Reference content and profile data remain source-separated. A referenced advertisement alone does not punish the current sender; explicit endorsement, contact, join, order, or similar promotion can. Warnings, reports, analysis, verification, sample requests, ordinary discussion, personal experience, and personal support are not promotion.

Product/service pitches, private-group recruitment, public-group/channel recruitment, project recruitment, payment-code recruitment, sexual offers, income recruitment, and cryptocurrency solicitation are evaluated by their respective semantic conditions. A product's features or warranty can form a short sales pitch; isolated keywords, profile text, links, coin names, payment terms, or quoted advertisements do not independently establish spam.

The inclusive score threshold is the `SPAM_THRESHOLD` value maintained in `src/anti_fwd_spam/constants.py`. A score at or above it uses Action 1. A lower score, invalid answer, missing provider, or failed request leaves the message visible.

## Edits and retries

An edit fences older model work for that message. An edited message still passes source and report routing. An edited message without a reference can use local rules; a referenced edit starts a fresh Jev classification. Edits do not rerun automatic account-blacklist checks.

Webhook delivery retries and scheduled task retries are separate. Durable task leases prevent concurrent work from repeating a completed moderation action. A confirmed deletion or ban is not repeated after a later redelivery; uncertain operations remain retryable or require manual inspection.

## Stored state and privacy

The bot stores report evidence, moderation progress, bounded pending model input, message identifiers, and long-lived source/account/sticker blacklists in D1. Recent-message indexes contain identifiers and timestamps rather than message text. Pending model content is cleared after classification finishes. Retention and cleanup are implementation-owned; operators should keep database exports and member information private.
