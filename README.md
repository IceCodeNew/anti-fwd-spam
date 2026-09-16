# anti-fwd-spam

群管理员可以用这个 Telegram bot 自动删除指定来源 bot 的垃圾消息，也可以通过回复消息并 @ bot 举报。管理员可将它部署到自己的 Cloudflare 账户。

## 创建 bot 并加入群组

1. 在 [@BotFather](https://t.me/BotFather) 中发送 `/newbot`，按提示创建 bot，保存 token 和用户名。
2. 在 BotFather 中为该 bot 关闭 Group Privacy Mode；需要接收其他 bot 的消息时，按 [Telegram 的设置说明](https://core.telegram.org/api/bots/bot-to-bot)启用 Bot-to-Bot Communication Mode。
3. 将 bot 加入需要管理的群组，设为管理员，并授予「删除消息」和「封禁用户」权限。后者用于禁言，bot 不会移出成员。
4. 对于频道评论区，将 bot 加入频道关联的讨论群。永久禁言适用于超级群（包括频道讨论群）；普通群只执行删除。

## 部署到 Cloudflare

### 安装工具并登录

先准备 Cloudflare 账户，安装 [Git](https://git-scm.com/downloads) 和 [mise](https://mise.jdx.dev/getting-started.html)。以下命令在 Bash 中执行；凭据设置和 webhook 注册使用同一个终端。

```bash
git clone https://github.com/IceCodeNew/anti-fwd-spam.git
cd anti-fwd-spam
mise trust
mise install
mise exec -- uv sync --locked
mise exec -- npm ci --ignore-scripts
mise exec -- uv run pywrangler login
```

希望避免付费时，保持 Cloudflare Free 计划，并在控制台查看用量。[Workers](https://developers.cloudflare.com/workers/platform/pricing/) 和 [D1](https://developers.cloudflare.com/d1/platform/pricing/) 的额度以官方价格页为准。

### 配置消息来源和数据库

打开 [`wrangler.jsonc`](wrangler.jsonc)，修改 `vars` 中的设置：

| 设置 | 填写方式 |
| --- | --- |
| `BLACKLIST_BOT_IDS` | 需要拦截的来源 bot 的数字 ID，以英文逗号分隔。不要填写群 ID、普通成员 ID 或用户名。 |
| `BOT_USERNAME` | 第一步创建的管理 bot 的用户名，不含 `@`。改名后也要更新此处。 |

创建举报数据库：

```bash
mise exec -- uv run pywrangler d1 create anti-fwd-spam-reports
```

将命令返回的 `database_id` 填入 `wrangler.jsonc` 的 `d1_databases`，替换仓库中的 ID。保留 `binding` 为 `REPORTS`，然后创建数据表：

```bash
mise exec -- uv run pywrangler d1 migrations apply anti-fwd-spam-reports --remote
```

已有部署时，继续使用原数据库 ID，跳过数据库创建命令。

### 保存凭据并发布

输入 BotFather 提供的 token。终端不会显示输入内容：

```bash
read -rsp 'Bot token: ' BOT_TOKEN; printf '\n'
TELEGRAM_WEBHOOK_SECRET="$(mise exec -- python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s' "$BOT_TOKEN" | mise exec -- uv run pywrangler secret put BOT_TOKEN
printf '%s' "$TELEGRAM_WEBHOOK_SECRET" | mise exec -- uv run pywrangler secret put TELEGRAM_WEBHOOK_SECRET
mise exec -- uv run pywrangler deploy
```

保存发布命令返回的 HTTPS 地址。不要将 token 或 webhook 密钥写入仓库、截图或公开日志。

### 注册 webhook

将发布地址填入 `WORKER_URL`，不带末尾的 `/`。以下操作会替换该 bot 原有的 webhook；不要与另一套 bot 服务共用 token。

```bash
read -rp 'Worker HTTPS URL: ' WORKER_URL
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WORKER_URL%/}/webhook" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","edited_message"]'
unset TELEGRAM_WEBHOOK_SECRET
```

确认返回 JSON 中的 `ok` 和 `result` 都为 `true`。后续只更新规则或代码、未更改地址和密钥时，重新执行 `mise exec -- uv run pywrangler deploy` 即可，不必再次注册 webhook。

## 举报垃圾消息

1. 在群里长按或右键目标消息，选择「回复」。
2. 输入 `@`，从 Telegram 的候选列表中选择已部署的管理 bot，然后发送。

普通成员举报时，bot 只保存证据。群主或管理员举报时，bot 会删除目标消息，并永久禁言超级群中的非管理员发送者。管理员应使用个人身份举报，匿名管理员举报不会处理。

bot 只删除目标消息，保留发送者的其他消息。群主、管理员和以群组或频道身份发言的发送者不会被禁言。

自动拦截只适用于 Telegram 保留了指定 bot 来源的内联消息或转发消息。复制粘贴的内容、隐藏来源的转发可能无法自动识别，此时可按上述步骤举报。

## 在测试群验证

在测试超级群或频道讨论群中，使用一个非管理员测试账号，先发送一条普通消息，再通过黑名单中的来源 bot 发送一条内联消息。确认只有后一条被删除，测试账号无法继续发言，前一条消息仍在。

接着在群管理的成员权限设置中解除该账号的禁言，再发送一条测试垃圾消息。先用普通成员账号回复并 @ 管理 bot，确认消息保留；再用管理员账号举报，确认目标消息被删除、发送者被禁言。

测试管理员保护时，让管理员发送测试消息，再举报该消息。确认消息被删除，但该管理员仍能发言。每轮测试后手动解除测试账号的禁言。

## 检查 webhook 和处理失败

在仍保存 `BOT_TOKEN` 的终端运行；换了终端时，先重新执行上面的 `read -rsp` token 输入命令。

```bash
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

检查 `result.url` 是否等于发布地址加 `/webhook`。发送测试消息后再次查询：`pending_update_count` 持续增加或出现新的 `last_error_message` 时，打开 Cloudflare 控制台中的该 Worker，查看日志。

- 遇到 `401`，重新保存 webhook 密钥，并用同一个值执行「注册 webhook」。
- 遇到 `404`，核对地址是否包含 `/webhook`。
- 删除或禁言失败时，核对 bot 的管理员权限，以及目标发送者是否为管理员或匿名身份。
- 举报存储失败时，在 Cloudflare 控制台检查 `REPORTS` 绑定、数据库迁移和 D1 用量。

`pending_update_count` 为 `0` 仅表示没有积压，仍需在测试群确认删除与禁言结果。处理完后运行 `unset BOT_TOKEN`，清除当前终端变量。

## 查看举报记录

在 Cloudflare 控制台打开 D1 数据库 `anti-fwd-spam-reports`，查看 `reports` 表。`raw_update` 保存 Telegram 投递的完整举报 JSON，包含被回复的消息；`classification` 保存消息类型和媒体字段分类。查看或导出时，避免公开成员信息。

举报记录保留 3 天，不下载图片或其他媒体文件。定时任务清理到期记录；服务故障或额度耗尽可能延迟删除。Cloudflare 的备份另有保留周期，具体见 [D1 Time Travel](https://developers.cloudflare.com/d1/reference/time-travel/)。
