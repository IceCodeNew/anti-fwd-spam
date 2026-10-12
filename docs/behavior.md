# Message processing and reporting

This is the bot's stable behavior contract. It describes what Telegram users and trusted operators can expect from an authenticated update. Deployment steps belong in [setup](setup.md); routine operations belong in [operations](operations.md); development and extension guidance belongs in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Scope and authorization

The Worker accepts authenticated Telegram `message` and `edited_message` updates at `POST /webhook`. Invalid content is rejected without moderation. Private messages are ignored except for supported commands.

A reporting identity is either a configured user ID or a configured sender-chat ID. A sender-chat identity is evaluated on its own; a compatibility `from` field cannot authorize it. An ordinary user must be a genuine non-bot sender whose ID is configured. Authorization is checked again on redelivery and retry.

- A listed `/bs` command is handled before automatic filtering. `/bs @username` registers the named source without immediately moderating a reply target. A bare `/bs` reply registers the target's inline bot, or its actual bot sender when there is no inline bot; registration must succeed before the target and registered source receive Action 2. The guest caller is not a source or moderation target. Sticker registration and target validation still apply.
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
6. Apply local text, profile, sticker, and maintenance rules to messages without a reference and to actual bot senders even when referenced.
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
    J -->|No report| R{Actual bot sender?}
    R -->|Yes| O[Existing local rules on current message]
    R -->|No| M{Reference present?}
    M -->|Yes| N[Jev with bounded reference context]
    M -->|No| O
    O -->|Match| P[Action 1 on current sender]
    O -->|No match, human sender| N
    O -->|No match, bot sender| Q[Leave message unchanged]
    N -->|Spam at threshold| P
    N -->|Below threshold or unavailable| Q[Leave message unchanged]
```

Human messages with a same-chat reply, external reply, or quote bypass local regex and sticker-list checks so Jev can interpret their context. Actual bot senders, including guest bots, still undergo the existing local checks on their own message even when replying; they do not enter Jev. Referenced content and guest caller metadata never substitute for the current sender's content or identity. A forum topic attachment that is not an explicit reply does not count as a reference.

## Moderation decision map

The two moderation actions have different targets. Source identities include the actual bot sender, an inline bot, and visible forwarded bot origins; a compatibility `from` attached to `sender_chat` and guest caller metadata are never source identities. A source match handles the sender and each listed source independently. When a direct bot is both sender and source, Action 2 targets that same bot, not its caller. A trusted report handles the explicitly reported account; an automatic content match handles only the current sender.

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

Local checks run before Jev on messages without a reference and on actual bot senders with references. General local patterns inspect authored text, captions, shared contact display names, and sender nicknames; deferred profile checks inspect the available sender and contact-account biographies after Telegram lookups. A separate maintenance pattern checks only current text and captions for a same-sentence claim that a group or channel is disabled, moved, or replaced followed by a request to join a new destination. Administrators are protected before Action 1, so ordinary administrator notices remain intact.

A local match ends model classification for that message. The same local regex and sticker checks apply to new and edited bot messages regardless of a reference; human reference messages keep the Jev path. Floods found only in referenced content do not count as a local match. Bots receive deletion only for a content match, while administrator/anonymous-group protection still applies. A registered source match uses Action 2 on the bot separately from content filtering.

## Jev classification

Jev is optional. When enabled, the bot sends the current sender's bounded message context, selected profile data, contact display data, and bounded reference context to the configured provider. It does not download or inspect media. Provider failures are retried according to the durable task policy; invalid or unavailable model answers do not count as spam.

The current sender's intent is the primary evidence. Reference content and profile data remain source-separated. A referenced advertisement alone does not punish the current sender; explicit endorsement, contact, join, order, or similar promotion can. Warnings, reports, analysis, verification, sample requests, ordinary discussion, personal experience, and personal support are not promotion.

Product/service pitches, private-group recruitment, public-group/channel recruitment, project recruitment, payment-code recruitment, sexual offers, income recruitment, and cryptocurrency solicitation are evaluated by their respective semantic conditions. A product's features or warranty can form a short sales pitch; isolated keywords, profile text, links, coin names, payment terms, or quoted advertisements do not independently establish spam.

The inclusive score threshold is the `SPAM_THRESHOLD` value maintained in `src/anti_fwd_spam/constants.py`. A score at or above it uses Action 1. A lower score, invalid answer, missing provider, or failed request leaves the message visible.

## Edits and retries

An edit fences older model work for that message. An edited message still passes source and report routing. An edited human message without a reference can use local rules; an eligible referenced human edit starts fresh Jev classification. Bot messages use local rules even when referenced and never enter Jev. Edits do not rerun automatic account-blacklist checks.

Webhook delivery retries and scheduled task retries are separate. Durable task leases prevent concurrent work from repeating a completed moderation action. A confirmed deletion or ban is not repeated after a later redelivery; uncertain operations remain retryable or require manual inspection.

## Stored state and privacy

The bot stores report evidence, moderation progress, bounded pending model input, message identifiers, and long-lived source/account/sticker blacklists in D1. Recent-message indexes contain identifiers and timestamps rather than message text. Pending model content is cleared after classification finishes. Retention and cleanup are implementation-owned; operators should keep database exports and member information private.
