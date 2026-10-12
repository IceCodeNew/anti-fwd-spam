# anti-fwd-spam

English | [简体中文](README_zh-Hans.md)

A Telegram moderation bot for Cloudflare Workers and D1. It handles reports, blocks messages from registered source bots, tracks account and sticker blacklists, and can classify ambiguous text with Jev.

## Is it for you?

Use this bot when you need moderation inside Telegram groups and are comfortable with:

- Cloudflare Workers and D1 for hosting and storage.
- Telegram administrator permissions for deleting messages and restricting members.
- A separate model provider when you enable Jev classification.

Use a **supergroup** for the complete feature set. Basic groups can delete messages but cannot provide permanent mutes. Administrators and group owners are protected from automatic mutes and bans. A confirmed ban or deletion can remove history that Telegram cannot restore.

## What the bot does

| Situation | Result |
| --- | --- |
| An authorized reporter replies to a spam message and mentions the bot | Ban the reported sender and clean eligible indexed history. |
| An unlisted member submits a valid reply report | Save evidence without automatic punishment. |
| A message comes from a registered source bot | Delete the current message; moderate the source account separately. |
| A new message matches a local rule or registered sticker | Delete the current message and mute its sender when Telegram supports it. |
| Jev classifies a message as spam | Delete the current message and mute its sender; referenced authors are not punished. |
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
- **Inspect or remove a false positive:** follow [Operations](docs/operations.md), then lift the Telegram restriction separately if needed.

Reporting identity, administrator protection, source attribution, sticker handling, and false-positive recovery are defined by [the behavior contract](docs/behavior.md).

## Optional Jev classification

Jev is used after local rules, blacklist checks, sticker checks, and report handling. It receives the bounded message context selected by the bot; media is not downloaded or inspected. A provider may charge for requests and may receive user-provided text and profile data, so review its terms before enabling the feature.

The model uses the current sender's intent as the primary evidence. Referenced content and profile data remain source-separated. A narrow project-recruitment hint is added only when the current message and sender biography both support that interpretation. A score at or above the configured inclusive threshold triggers the same current-message moderation action; see [Jev checks](docs/jev-checks.md) for the validation procedure.

Read [Jev checks](docs/jev-checks.md) before running live model tests. Provider configuration belongs in deployment secrets, not in repository files.

## Documentation map

- [Deployment setup](docs/setup.md) — create the bot, database, secrets, webhook, and deployment.
- [Operations](docs/operations.md) — reports, source/sticker lists, false positives, troubleshooting, privacy, and retention.
- [Behavior contract](docs/behavior.md) — final routing, authorization, moderation, retry, and storage behavior.
- [Jev checks](docs/jev-checks.md) — safe, repeatable live-classification procedure and fixture rules.
- [Contributing and maintenance](CONTRIBUTING.md) — development commands, module boundaries, and change requirements.

## Contributing

Read [Contributing and maintenance](CONTRIBUTING.md) before changing behavior. Keep behavior changes, behavior tests, and the contract synchronized. Run the documented checks before opening a pull request.
