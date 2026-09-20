# anti-fwd-spam

[English](README.md) | 简体中文

部署在 Cloudflare Workers 上的 Telegram 群管理机器人，使用 Cloudflare D1 保存黑名单和举报记录。管理员可以举报垃圾消息、拦截指定来源 bot 的消息，也可以启用 Jev 模型检查新消息。

请先在测试群中使用。封禁可能删除历史消息，解除封禁无法恢复已删除的内容。

## 1. 创建 Telegram bot

1. 向 [@BotFather](https://t.me/BotFather) 发送 `/newbot`，按提示创建 bot，保存 token 和用户名。
2. 在 BotFather 中关闭 Group Privacy Mode（群组隐私模式）。需要接收其他 bot 的消息时，也启用 [Bot-to-Bot Communication Mode](https://core.telegram.org/api/bots/bot-to-bot)。
3. 将 bot 加入群组，设为管理员，授予「删除消息」和「封禁用户」权限。管理频道评论时，将 bot 加入频道关联的讨论群。

使用超级群（例如频道关联的讨论群）可启用完整的管理功能。Telegram 普通群不支持永久禁言。

## 2. 准备部署工具

注册 [Cloudflare 账户](https://dash.cloudflare.com/sign-up)，安装 [Git](https://git-scm.com/downloads) 和 [mise](https://mise.jdx.dev/getting-started.html)。Windows 用户使用 WSL 中的 Linux 终端。以下命令在 Bash 中执行，请保持使用同一个终端窗口。

```bash
git clone https://github.com/IceCodeNew/anti-fwd-spam.git
cd anti-fwd-spam
mise trust
mise install
mise exec -- uv sync --locked
mise exec -- npm ci --ignore-scripts
mise exec -- uv run pywrangler login
```

登录命令会打开浏览器，请登录用于部署 bot 的 Cloudflare 账户。某条命令失败时，先处理错误，再继续后续步骤。

Cloudflare 为 [Workers](https://developers.cloudflare.com/workers/platform/pricing/) 和 [D1](https://developers.cloudflare.com/d1/platform/pricing/) 提供免费额度。部署前查看额度限制，使用中在控制台查看用量；模型服务单独计费。

## 3. 创建数据库

```bash
mise exec -- uv run pywrangler d1 create anti-fwd-spam-reports
```

用文本编辑器打开 `wrangler.jsonc`，将 `d1_databases` 中的 `database_id` 替换为命令返回的 ID。保留 `binding` 为 `REPORTS`，保留其他设置，包括 `triggers` 中的定时任务。

创建数据库表：

```bash
mise exec -- uv run pywrangler d1 migrations apply anti-fwd-spam-reports --remote
```

## 4. 配置 bot 和举报身份

输入 BotFather 提供的 token，终端不会显示输入内容。完成 webhook 注册前，不要关闭这个终端。

```bash
read -rsp 'Bot token: ' BOT_TOKEN; printf '\n'
```

注册 webhook 前，先用每个需要举报权限的账号给新 bot 发一条私聊消息，然后执行：

```bash
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/getUpdates"
```

在返回的 `result` 中找到对应消息，复制 `message.from.id`。授权匿名管理员时，以群组匿名身份发一条提及 bot 的消息，复制该消息的 `message.sender_chat.id`。结果为空时，发送一条新消息后重新执行命令。输出中包含成员信息，不要公开。

将以下设置保存为 Worker secret（运行时密钥）。每条 `secret put` 命令都会提示输入对应的值。Wrangler 询问是否创建 Worker 时，确认创建：

```bash
mise exec -- uv run pywrangler secret put BOT_USERNAME
mise exec -- uv run pywrangler secret put REPORTER_IDS
printf '%s' "$BOT_TOKEN" | mise exec -- uv run pywrangler secret put BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET="$(mise exec -- python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s' "$TELEGRAM_WEBHOOK_SECRET" | mise exec -- uv run pywrangler secret put TELEGRAM_WEBHOOK_SECRET
```

| 设置 | 填写内容 |
| --- | --- |
| `BOT_USERNAME` | 管理 bot 的用户名，不含 `@`。 |
| `REPORTER_IDS` | 可信用户或发言所用的聊天身份数字 ID，用英文逗号分隔。例如 `123456789,-1001234567890`，请替换两个示例 ID。 |

只有名单中的身份可以举报或使用 `/bs`，群主也不例外。以群组或频道身份发言时，授权按该发言身份判断，不按背后的个人账号判断。加入群组身份不会授权群成员的个人账号。留空则禁用举报和 `/bs`。

不要将 token 和密钥写入仓库文件、截图或公开日志。

## 5. 部署并连接 Telegram

```bash
mise exec -- uv run pywrangler deploy
```

复制命令输出的 Worker HTTPS 地址，将它注册为 Telegram 的 webhook。Telegram 会向这个地址发送 bot 收到的消息：

```bash
read -rp 'Worker HTTPS URL: ' WORKER_URL
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${WORKER_URL%/}/webhook" \
  --data-urlencode "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","edited_message"]'
unset TELEGRAM_WEBHOOK_SECRET
```

确认返回内容包含 `"ok":true` 和 `"result":true`。同一个 token 只用于一套 bot 部署；注册另一个 webhook 会替换此地址。

## 举报和管理消息来源

### 举报消息

使用 `REPORTER_IDS` 中的身份，选择垃圾消息的「回复」，输入 `@`，选中管理 bot 后发送。

在超级群中，管理员或已授权的群组、频道身份举报后，bot 删除目标消息、封禁发送者，并清理符合条件的索引历史。清理成功后，bot 删除举报消息。已授权普通成员的举报只保存证据，不删除消息或限制成员。

确认封禁后，发送者进入账号黑名单。该账号在使用同一 bot 的其他超级群发言时，bot 会在该群封禁账号并清理符合条件的索引消息。

### 添加来源

用已授权身份在私聊或群聊中发送 `/bs @example_bot`，将 `example_bot` 替换为来源 bot 的用户名。群内可以使用 `/bs@管理bot用户名 @example_bot` 明确指定接收命令的 bot。

bot 将解析出的账号 ID 保存到来源名单，并回复该 ID。群聊中，bot 回复后会删除命令消息，查询失败时也会清理；私聊命令和结果回复会保留。登记来源本身不封禁账号或删除历史。请核对返回的 ID：Telegram 无法解析部分用户名，命令也不校验目标是否为 bot。

有人通过名单中的 bot 发送内联消息，或转发带有该 bot 来源标记的消息时：

- 对发送者，只删除当前消息并永久禁言，保留其他历史消息，不加入黑名单。
- 对来源 bot，执行封禁、加入账号黑名单，并清理该 bot 符合条件的索引消息。

复制粘贴的内容、隐藏来源的转发，以及普通账号直接发送的消息，不匹配来源名单。

### 管理员保护和历史清理范围

群主和管理员免于禁言、封禁，来源 bot 是管理员时也受保护。来源和账号黑名单匹配不会删除其消息。获授权管理员举报仍可删除指定的管理员消息。

历史清理只覆盖 bot 在同一群中收到并建立索引的消息，范围截至触发消息或在举报之前，且距今不足 48 小时。bot 无法搜索未收到的历史。Telegram 可能在封禁时删除更多历史消息。

### 查看名单和解除误报

在 Cloudflare 控制台打开 **D1 → anti-fwd-spam-reports → Console**，查询名单：

```sql
SELECT source_id FROM blacklisted_sources ORDER BY source_id;
SELECT bot_id, user_id FROM blacklisted_users ORDER BY bot_id, user_id;
```

`blacklisted_sources` 保存登记的来源 ID；`blacklisted_users` 按管理 bot 保存封禁账号 ID。从查询结果复制需要移除的 ID，替换以下示例。来源 bot 可能同时出现在两张表中，需要从两张表中移除，才能停止两类拦截：

```sql
DELETE FROM blacklisted_sources WHERE source_id = 987654321;
DELETE FROM blacklisted_users WHERE bot_id = 123456789 AND user_id = 987654321;
```

然后在各个受影响群组的 Telegram 成员设置中解除禁言或封禁。删除数据库条目不会解除 Telegram 限制。账号仍在账号黑名单中时，再次发消息会触发封禁。

## 可选：启用 Jev 垃圾消息检查

启用后，bot 会向外部模型平台发送用户昵称、可获取的简介、正文或媒体说明，以及格式信息和选定的媒体描述，不下载或识别媒体内容。启用前请查看平台的隐私条款和价格。

将 API key 保存为对应的 Worker secret。配置多个平台时，bot 从下表中第一个已配置的平台开始请求，失败时可能切换平台：

| 平台 | Worker secret |
| --- | --- |
| [TypeSafe AI](https://ai-sdk.dev/providers/ai-sdk-providers/typesafe-ai) | `TYPESAFE_AI_API_KEY` |
| [Vercel AI Gateway](https://vercel.com/ai-gateway/models/jev) | `AI_GATEWAY_API_KEY` |
| [Experiential](https://platform.experientiallabs.ai/models/jev-latest) | `EXPERIENTIAL_API_KEY` |
| [OpenCode Zen](https://opencode.ai/docs/zen/) | `OPENCODE_API_KEY` |

使用 Vercel 时执行：

```bash
mise exec -- uv run pywrangler secret put AI_GATEWAY_API_KEY
```

bot 在其他管理规则之后，用 Jev 检查用户新消息。垃圾消息分数达到阈值时，只删除该条消息，不禁言、不封禁、不添加黑名单。阈值见 [model.py](src/anti_fwd_spam/model.py) 中的 `SPAM_THRESHOLD`；分数不代表准确率。编辑消息、服务事件、bot 消息及以群组或频道身份发送的消息不参与检查。

临时失败时，bot 保留消息，等待定时重试。请保留定时任务。每次请求都可能产生费用，也可能向另一个已配置平台发送相同内容。

关闭检查并暂停待处理的模型任务时，删除已配置的全部模型 secret。只配置了 Vercel 时执行：

```bash
mise exec -- uv run pywrangler secret delete AI_GATEWAY_API_KEY
```

## 验证运行和排查故障

使用可丢弃的测试群和测试账号。先用已授权管理员举报测试消息，确认目标消息删除、发送者封禁。单独测试来源过滤：私聊 bot 添加来源，用非管理员账号发送该来源的内联消息，确认消息删除、发送者禁言，之前的消息保留。测试结束后，移除测试黑名单条目并解除限制。

在保存了 `BOT_TOKEN` 的终端检查 Telegram 投递状态：

```bash
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

`result.url` 应等于 Worker 地址加 `/webhook`。`pending_update_count: 0` 只表示没有积压，不能证明管理功能正常。积压持续增加或出现新的 `last_error_message` 时，在 Cloudflare 控制台打开 Worker 日志。

| 现象 | 检查方法 |
| --- | --- |
| 举报或 `/bs` 无反应 | 核对 `REPORTER_IDS`、发言身份和 `BOT_USERNAME`。 |
| webhook 返回 `401` | 用同一个 `TELEGRAM_WEBHOOK_SECRET` 保存密钥并注册 webhook。 |
| webhook 返回 `404` | 使用以 `/webhook` 结尾的部署地址。 |
| 删除、禁言或封禁失败 | 核对 bot 的管理员权限，以及目标是否受管理员保护。 |
| `ban confirmation failed` 或 `mute retry skipped` | 先手动核对成员状态，再决定是否重新举报或修改限制。 |
| `history cleanup rejected` | 手动检查并删除残留消息。 |
| 存储错误 | 检查 `REPORTS` 绑定、数据库表和 D1 用量。 |

换了终端时，先按第 4 步重新输入 `BOT_TOKEN`。使用结束后运行 `unset BOT_TOKEN`。

## 数据存储

D1 保留举报和来源处理的完整 JSON、命令解析出的 ID，以及临时处理记录，保留期为 3 天。近期消息索引只保存 ID 和时间，不保存正文。模型任务暂存提交给模型的内容。bot 不下载媒体文件。

来源和账号黑名单长期保留，直到手动删除。定时任务清理到期记录，服务故障或额度耗尽可能延迟清理。Cloudflare 备份另有[保留规则](https://developers.cloudflare.com/d1/reference/time-travel/)。查看或导出数据库时，不要公开成员信息。
