# Message processing and reporting

This is the bot's stable behavior contract. It describes what Telegram users and trusted operators can expect from an authenticated update. Deployment steps belong in [setup](setup.md); routine operations belong in [operations](operations.md); development and extension guidance belongs in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Scope and authorization

The Worker accepts authenticated Telegram `message` and `edited_message` updates at `POST /webhook`. Invalid content is rejected without moderation. Private messages are ignored except for supported commands.

A reporting identity is either a configured user ID or a configured sender-chat ID. A sender-chat identity is evaluated on its own; a compatibility `from` field cannot authorize it. An ordinary user must be a genuine non-bot sender whose ID is configured. Authorization is checked again on redelivery and retry.

- A listed `/bs` command is handled before automatic filtering. `/bs @username` registers the named source without immediately moderating a reply target. A bare `/bs` reply registers the target's inline bot, or its actual bot sender when there is no inline bot; registration must succeed before the target and registered source receive Action 2. The guest caller is not a source or moderation target. Sticker registration and target validation still apply.
- A listed `/allow @username` or `/allow <account ID>` in a supergroup removes the selected account from this bot's account blacklist and the shared source blacklist and restores its access there. A kicked account is unbanned; a restricted member's personal restrictions are lifted without changing group-wide defaults. Telegram restoration confirmation precedes blacklist removal. Ordinary members, administrators, other groups' bans/restrictions, other bots' account entries, and sticker lists are unchanged. Recorded usernames are checked against the account's current profile before first resolution, then stay pinned on retry; unresolved, renamed or ambiguous identities require the numeric ID. `/allow` uses the same configured reporting identities as `/bs`, not administrator status alone; edited, private-chat and basic-group forms do not change state.
- An unlisted bare `/bs` reply to a valid group target records evidence but does not register a source or punish an account.
- An unlisted reply that mentions the bot records evidence and ends routing.
- An explicit unlisted `/bs` argument continues through ordinary filtering instead of registering a source.
- A report target must be a valid message in the same group; a forum topic's creation message is not a target.

## Routing order

For a new group message, processing follows this order:

1. Validate the update and authenticated webhook request.
2. Dispatch authorized commands before automatic filtering.
3. Route a join notice to independent checks of its new members, rather than its inviter.
4. For other messages, match explicit source identities and account blacklists.
5. Index an eligible message for possible later history cleanup, then apply reply reports and remaining command handling.
6. Apply all current-message text, nickname, contact-name and maintenance regexes before Jev for human senders below the mature-message threshold, with or without a reference. Established senders use Jev for regex matches. Sticker checks retain their existing reference boundary, and bot senders retain local-only filtering.
7. Queue Jev classification for the remaining eligible input.
8. Leave the message unchanged when no rule or model decision reaches the configured threshold.

```mermaid
flowchart TD
    A[Authenticated Telegram update] --> B{Supported command?}
    B -->|Listed /bs or /allow| C[Authorize and handle command]
    B -->|No| D{Group message?}
    D -->|No| E[Ignore, except commands]
    D -->|Yes| S{New members notice?}
    S -->|Yes| T[Check each joined account independently]
    T --> U{Listed account/source or profile advertisement?}
    U -->|Yes, unprotected| V[Permanently ban joined account and remove notice]
    U -->|No or protected| Q[Leave member unchanged]
    S -->|No| F[Match source and account blacklists]
    F -->|Source match| G[Action 1 on sender and Action 2 on source]
    F -->|Account match| H[Action 2 on account]
    F -->|No blacklist match| I[Index eligible message]
    I --> J{Reply report?}
    J -->|Trusted| K[Action 2 on reported sender]
    J -->|Unlisted| L[Save evidence only]
    J -->|No report| R{Actual bot sender?}
    R -->|Yes| O[Existing local rules on current message]
    R -->|No| M{Ten naturally matured observations?}
    M -->|No| W[Current-message regex checks, including replies]
    W -->|Match| P[Action 1 on current sender]
    W -->|No match| X[Existing sticker boundary and own-profile checks]
    M -->|Yes| Y[Existing sticker boundary, no direct regex punishment]
    X -->|Local match| P
    X -->|No match| N[Jev with bounded current and reference context]
    Y -->|Sticker match| P
    Y -->|No match| N
    O -->|Match| P[Action 1 on current sender]
    O -->|No match, human sender| N
    O -->|No match, bot sender| Q[Leave message unchanged]
    N -->|Spam at threshold| P
    N -->|Below threshold or unavailable| Q[Leave message unchanged]
```

A human sender's mature-message count selects one policy for all text/profile regexes. Below the threshold, a match in current text, caption, nickname, contact name or available own/contact biography can trigger Action 1 before Jev, including on replies and quotes. This intentionally stricter low-history policy may also match warnings or exact examples; operators can recover the account with `/allow`. At or above the threshold, regex matches are evidence for Jev rather than direct punishment. Source-separated reference context remains available to the model.

Referenced text, reference-author profiles and guest caller metadata never count as current-sender regex evidence. Existing command and reply-report routing takes precedence. A campaign-like text match neither registers its named bot as a source nor punishes that bot or the referenced author. Human reference messages still bypass sticker-list checks; this membership policy does not broaden sticker enforcement.

Actual bot senders, including guest bots, still undergo the existing local checks on their own message even when replying; they do not enter Jev. Referenced content and guest caller metadata never substitute for the current sender's content or identity. A forum topic attachment that is not an explicit reply does not count as a reference.

## Moderation decision map

The two moderation actions have different targets. Source identities include the actual bot sender, an inline bot, and visible forwarded bot origins; a compatibility `from` attached to `sender_chat` and guest caller metadata are never source identities. A source match handles the sender and each listed source independently. When a direct bot is both sender and source, Action 2 targets that same bot, not its caller. A trusted report handles the explicitly reported account; an automatic content match handles only the current sender.

```mermaid
flowchart LR
    A[Moderation trigger] --> B{Trigger type}
    B -->|Registered source| C[Current sender A: Action 1]
    B -->|Registered source| D[Source bot B: Action 2]
    B -->|Trusted reply report| E[Reported account A: Action 2]
    B -->|Local rule, sticker, or Jev on a message| F[Current sender A: Action 1]
    B -->|Joined account match| M[Joined member: Action 2 and notice removal]
    G[Unlisted report] --> H[Evidence only]
    C --> I{Protected administrator or owner?}
    F --> I
    I -->|Yes| J[Keep message/account protected]
    I -->|No| K[Delete current message and mute when supported]
    D --> L[Ban and clean eligible indexed history]
    E --> L
    M --> L
```

## Moderation outcomes

### Action 1: remove the current message and mute its sender

Local rules, sticker matches, and Jev spam matches use Action 1. The bot removes only the triggering message and permanently restricts a human sender when Telegram supports that operation. It does not clean history or add a blacklist entry. Bots are not muted. Basic groups support deletion but not permanent mute.

Group owners, administrators, and messages sent as a group's anonymous identity are protected from automatic Action 1. A valid trusted report may still delete an explicitly reported administrator message.

### Action 2: ban an account and clean eligible history

Trusted reply reports, explicit source reports, and matching joined accounts use Action 2. The bot bans the selected account when allowed, prevents rejoining, and removes eligible indexed messages in that group. A reported target message is deleted independently of the ban result when Telegram permits it. Confirmed bans add the account to the bot-scoped account blacklist.

Action 2 never treats the source bot as the sender's history unless the command explicitly reports both accounts. Target administrators remain protected from automatic bans and history cleanup.

## Joining accounts

A new `new_chat_members` service notice in a supergroup checks each joined account independently. The inviter or service-message actor is neither classified nor indexed as a joiner. Duplicate member identities are processed once; the moderation bot itself is skipped. Edited join notices and other service events do not create join checks.

Existing account blacklists and registered bot sources apply to joined accounts even before they speak. Otherwise, the bot checks a human joiner's own bounded nickname and available biography against existing profile-safe local rules and, when configured, a separate Jev profile-advertising scope. A nickname or biography that clearly solicits services, goods, sexual offers, cryptocurrency trades, or recruitment can be sufficient without an authored message. Isolated keywords, emoji, student identities, ordinary self-description, warnings, fictional dialogue, or advertisements in linked targets alone are insufficient. Unknown profile fields are not evidence of spam. Unlisted bots do not enter profile classification.

A match permanently bans only that joined account, preventing rejoining, and records the confirmed ban in the existing bot-scoped account blacklist. It also removes the shared join notice and cleans eligible indexed history of that account, not the inviter. Normal co-joiners stay in the group; administrators and owners remain protected. A confirmed ban is not repeated when notice deletion needs retry. Each member has an independent durable task and moderation subject, so one failed lookup, decision, or action does not overwrite another's progress.

## Member maturity

Established membership means this bot has observed ten qualifying messages in this group that reached natural expiry in its history index. The persistent counter is scoped by bot, group and account and saturates at ten; it is not Telegram's complete lifetime history or a guarantee of benign intent. Missing counters start below the threshold; pre-migration rows are not credited because their eligibility is unknown.

New human-authored content can qualify. Bots, chat identities, commands, reply reports, service events and edit-only observations do not earn credit. Duplicate delivery and edits do not refresh a message's age or upgrade its eligibility. Automatic moderation disqualifies the triggering observation before deletion while retaining its deduplication key. Confirmed history cleanup retains ineligible removal tombstones until natural expiry, excludes them from later cleanup queries, and prevents replay from reintroducing credit. Only natural expiry earns credit, never receipt or moderation deletion.

Expiry aggregates credit and deletes exactly the same bounded set of observations in one transaction. A failed batch does neither, and retry cannot credit twice. The history window owned by `MESSAGE_WINDOW_SECONDS` in `evidence.py` is also the account-farming delay: shortening it changes how quickly an account can establish trust. Telegram's Bot API does not expose historical join time; this policy relies on aged observations rather than claiming verified continuous membership for 24 hours.

## Local filtering

Text/profile regex checks run before Jev for low-history human senders and actual bot senders, regardless of references. General local patterns inspect authored text, captions, shared contact display names, and sender nicknames; deferred profile checks inspect the available sender and contact-account biographies after Telegram lookups. A separate maintenance pattern checks only current text and captions for a same-sentence claim that a group or channel is disabled, moved, or replaced followed by a request to join a new destination. Administrators are protected before Action 1, so ordinary administrator notices remain intact.

Local patterns are grouped by content:

| Category | Matching conditions |
| --- | --- |
| Contact solicitation | An `@` handle of 5–32 ASCII letters, digits or underscores, whitespace, then `campaign_` and digits; or `看头像`, optional whitespace and `加`, followed by Q/q/扣/扣扣, not immediately followed by a Latin letter or a character in U+4E00–U+9FFF. |
| Repeated symbols | At least four identical 💰 or 🔴 symbols, optionally separated by whitespace. |
| Sexual solicitation | `极品`/`新人`/`童颜`/`嫩` followed by optional `小` and `萝莉` (not followed by `塔`); or any of `萝莉`/`约炮`/`包夜`/`外围`/`楼凤` immediately followed by `上门`/`包夜`/`资源`. Individual type or offer words do not suffice. |
| Attack services | Any of DDOS/CC/`攻击测试`, plus `攻击`, plus any of `接`/`打死`/`打穿`/`日死`/`日穿`. DDOS/CC are case-insensitive and cannot be embedded in other Latin words. `攻击测试` itself satisfies `攻击`; it is not a testing exemption. |
| Advertising distribution | `广告代发` with zero to two non-word characters or underscores between adjacent characters; or `代发` plus `24小时`/`24h`/`全天` (case-insensitive, allowing whitespace after `24`). |
| Bulk sales | `批量出售` or `批量出手`. |
| Income | Any of `日赚`/`日收`/`日撸`/`日入` plus any of `洗米`/`洗钱`/`手速快`/`来`/`莱`. Non-word separators, including invisible characters, are allowed within `手速快` and between `日` and its income verb. |
| Exact phrase | `只吸引对的人` as the exact whole field, without surrounding whitespace or punctuation. |

Co-occurrence (`plus`) is within one inspected field, in any order and across line breaks. Adjacent phrase components retain their stated order. Group migration notices remain a separate current-text/caption-only category as described above.

These local rules do not exempt warnings, questions or quoted wording embedded in the current sender's own text. A low-history human can therefore receive Action 1 for such discussion; `/allow` restores access but cannot restore a deleted message. Established humans receive semantic Jev classification instead. Text contained only in a Telegram reply or quote remains outside local matching. The obfuscated `承接广*告*戴*發` template is not a separate local rule.

A permitted local match ends model classification for that message. All text/profile regexes share the mature-member gate; established humans instead receive Jev classification. The same local regex and sticker checks apply to new and edited bot messages regardless of a reference. Floods found only in referenced content do not count as a local match. Bots receive deletion only for a content match, while administrator/anonymous-group protection still applies. A registered source match uses Action 2 on the bot separately from content filtering.

## Jev classification

Jev is optional. When enabled, the bot sends the current sender's bounded message context, selected profile data, contact display data, and bounded reference context to the configured provider. It does not download or inspect media. Provider failures are retried according to the durable task policy; invalid or unavailable model answers do not count as spam.

The current sender's intent is the primary evidence. Reference content and profile data remain source-separated. A referenced advertisement alone does not punish the current sender; endorsement, contact, join, order, or similar promotion can. Supplying only a recruitment contact in the current message can complete the contact funnel of a clearly advertising reference without an explicit recommendation verb. Isolated handles and contacts for ordinary job information are insufficient. Warnings, reports, analysis, verification, sample requests, ordinary discussion, personal experience, and personal support are not promotion, including when they identify an advertised contact.

Product/service pitches, private-group recruitment, public-group/channel recruitment, project recruitment, payment-code recruitment, sexual offers, income recruitment, and cryptocurrency solicitation are evaluated by their respective semantic conditions. Cross-border group-branded recruitment combining generalized promotion/merchant-acquisition roles, U-denominated bonuses or base pay, high commissions, and high combined earnings can constitute recruitment advertising without proof of fraud or daily-income promises. Ordinary local jobs, concrete overseas employment, and salary discussion are not classified by country, pay amount, currency, or job title alone. A product's features or warranty can form a short sales pitch; isolated keywords, profile text, links, coin names, payment terms, or quoted advertisements do not independently establish spam.

Payment-intermediary and laundering recruitment, funded gift-brushing tasks, card-redemption income schemes, and vague easy-income task recruitment count as advertisements even without a contact handle or a daily-income amount. Promises of easy, high monthly earnings do not make such recruitment an ordinary job. Attack-for-hire, advertising distribution, bulk account sales and API or subscription resale follow the same supplier-intent boundary as other services. Obfuscated spelling and invisible characters are interpreted in context, not treated as advertising evidence themselves. Ordinary merchant receipts, cooking, personal gift-giving, card support, concrete jobs, technical questions and warnings remain non-promotional. Isolated sales-like fragments without a discernible offer do not establish solicitation.

In message classification, direct attempts by the current sender to override the moderation instructions or force a particular score also count as removable spam, even without advertising. Security discussion, reports and quoted attack examples are not themselves attempts to control the current classification. The separate joining-profile scope continues to judge advertising intent rather than treating instruction-like profile text alone as advertising.

The inclusive score threshold is the `SPAM_THRESHOLD` value maintained in `src/anti_fwd_spam/constants.py`. A message score at or above it uses Action 1; the separate joining-account scope uses the join outcome above. A lower score, invalid answer, missing provider, or failed request leaves the message visible.

## Edits and retries

An edit fences older model work for that message. An edited message still passes source and report routing. An edited low-history human message without a reference can use immediate local rules but does not start fresh profile classification; an eligible referenced edit or established member's edit starts fresh Jev classification. Bot messages use local rules even when referenced and never enter Jev. Edits do not rerun automatic account-blacklist checks.

Webhook delivery retries and scheduled task retries are separate. Durable task leases prevent concurrent work from repeating a completed moderation action. A confirmed deletion or ban is not repeated after a later redelivery; uncertain operations remain retryable or require manual inspection.

Within retained evidence, `/allow` fences older source registration and moderation events and cancels superseded account tasks. Membership restoration is durably claimed before contacting Telegram, so concurrent deliveries cannot repeat it. Explicit rejection permits retry; an unknown remote outcome or lost success checkpoint retains the claim and requires manual inspection rather than another restoration. A newer same-group recovery, ban, or issued mute supersedes an older incomplete recovery. Completed command redelivery does not undo a subsequent new ban or mute. This is replay protection, not a permanent allowlist: a new advertisement, join, or trusted report can trigger moderation again. Already-issued Telegram requests require outcome handling; unresolved overlap requires manual inspection rather than silently claiming recovery. Deleting blacklist rows manually does not restore deleted history or lift Telegram restrictions. `/allow` explicitly restores current-group access, but it cannot restore removed messages. Already-issued overlapping mute requests require permission inspection rather than a blind compensating unmute.

## Stored state and privacy

The bot stores report evidence, moderation progress, bounded pending model input, message identifiers, and long-lived source/account/sticker blacklists in D1. Recent-message indexes contain identifiers, timestamps and activity eligibility rather than message text. Persistent member-activity rows contain only scoped identifiers and a capped mature-message count. Pending model content is cleared after classification finishes. Retention and cleanup are implementation-owned; operators should keep database exports and member information private.
