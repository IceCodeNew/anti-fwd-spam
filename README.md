# anti-fwd-spam

English | [简体中文](README_zh-Hans.md)

A Telegram moderation bot for Cloudflare Workers and D1. It handles reports, blocks messages from registered source bots, tracks account and sticker blacklists, and can classify ambiguous text with an optional decision model.

## Is it for you?

Use this bot when you need moderation inside Telegram groups and are comfortable with:

- Cloudflare Workers and D1 for hosting and storage.
- Telegram administrator permissions for deleting messages and restricting members.
- A separate model provider when you enable model classification.

Use a **supergroup** for the complete feature set. Basic groups can delete messages but cannot provide permanent mutes. Administrators and group owners are protected from automatic mutes and bans. A confirmed ban or deletion can remove history that Telegram cannot restore.

## What the bot does

| Situation | Result |
| --- | --- |
| An authorized reporter replies to a spam message and mentions the bot | Ban the reported sender and clean eligible indexed history. |
| An unlisted member submits a valid reply report | Save evidence without automatic punishment. |
| A message comes from a registered source bot | Delete the current message; moderate the source account separately. |
| A new message matches a local rule or registered sticker | Delete the current message and mute its sender when Telegram supports it. |
| The model classifies a message as spam | Delete the current message and mute its sender; referenced authors are not punished. |
| A joining account advertises through its nickname or profile | Permanently ban the matched member and delete the join notice, even before they speak. |
| A message is edited or a network request fails temporarily | Fence stale work and retry only the pending operation. |

See [the behavior contract](docs/behavior.md) for the complete routing and protection rules.

## Quick start

1. Create a Telegram bot and add it to a disposable test supergroup as an administrator with permission to delete messages and restrict members.
2. Prepare Cloudflare Workers, D1, Git, and `mise`.
3. Create the D1 database, apply migrations, save the required Worker secrets, deploy the Worker, and register its `/webhook` endpoint.
4. Configure trusted reporting identities before using `/bs` or automatic reports.

The complete copy-pasteable setup is in [Deployment setup](docs/setup.md). Start with a test group: reports and bans can have destructive effects.

## Everyday use

- **Report spam:** reply to the target message and mention the moderation bot from a trusted reporting identity.
- **Register a source:** send `/bs @source_bot` from a trusted identity. Registered sources include bots that send messages directly, including guest bots.
- **Block and report a bot source:** reply with bare `/bs` to a direct bot message, or to an inline message when both its sender and source should be handled.
- **Register a sticker set:** send its complete `https://t.me/addstickers/<set_name>` URL with `/bs`.
- **Undo a ban or mute:** from a trusted identity in the affected supergroup, send `/allow @username` or `/allow <account ID>` to remove its blacklist entries and restore access. See [Operations](docs/operations.md) for username lookup limits and other false-positive recovery.

Reporting identity, administrator protection, source attribution, sticker handling, and false-positive recovery are defined by [the behavior contract](docs/behavior.md).

Local text/profile regexes apply before model classification until a member has ten qualifying observed messages that have naturally aged out of the history index. Established members use model classification instead of direct regex punishment. This is a stricter low-history policy, including replies and quotes; matched messages may be removed even when they are examples or warnings. Reporting and administrator protections remain in place. See [the behavior contract](docs/behavior.md#member-maturity) for counting and retention boundaries.

## Optional model classification

Optional classification uses GPT-6 Luna Decisions when a supported provider key is configured. See [provider setup](docs/setup.md#optional-model-provider) for keys, routing, and cost boundaries, and [compatibility and migration](docs/setup.md#compatibility-and-migration) before upgrading a Jev deployment. Classification runs after local rules, blacklist checks, sticker checks, and report handling. It receives bounded text context and selected profile data; the bot does not download or inspect images, video, or audio.

The model uses the current sender's intent as the primary evidence, including visible contact-card pitches and button text/URLs. Callback data is excluded. Model classification distinguishes current caption/button warnings from the offers they reproduce; product-use and visible inventory pitches remain promotional for established members. Local signatures alone do not establish model intent: standalone identifiers, repeated symbols and slogans remain insufficient. Narrow local signatures additionally cover complete obfuscated advertising offers in current text/captions, a single exact sales-menu button, and one complete 20-label menu with an empty or numeric body and no other content payload. They retain the existing member-maturity and authority protections; see [local filtering](docs/behavior.md#local-filtering). Referenced content and profile data remain source-separated. A score at or above the configured inclusive threshold triggers the same current-message moderation action; see [live model checks](docs/decision-checks.md) for the validation procedure.

Message classification also treats direct attempts to override moderation instructions or force a score as spam. Discussing defenses or reporting a quoted prompt-injection example does not itself count as an attack.

Only valid model scores can trigger moderation; invalid answers and refusals do not. See the [behavior contract](docs/behavior.md#model-classification).

Read [live model checks](docs/decision-checks.md) before running provider tests. Provider configuration belongs in deployment secrets, not repository files.

## Documentation map

- [Deployment setup](docs/setup.md) — create the bot, database, secrets, webhook, and deployment.
- [Operations](docs/operations.md) — reports, source/sticker lists, false positives, troubleshooting, privacy, and retention.
- [Behavior contract](docs/behavior.md) — final routing, authorization, moderation, retry, and storage behavior.
- [Live model checks](docs/decision-checks.md) — safe, repeatable live-classification procedure and fixture rules.
- [Contributing and maintenance](CONTRIBUTING.md) — development commands, module boundaries, and change requirements.

## Contributing

Read [Contributing and maintenance](CONTRIBUTING.md) before changing behavior. Keep behavior changes, behavior tests, and the contract synchronized. Run the documented checks before opening a pull request.
