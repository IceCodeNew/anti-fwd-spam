# anti-fwd-spam

[English](README.md) | 简体中文

部署在 Cloudflare Workers 上的 Telegram 群管理机器人，使用 Cloudflare D1 保存黑名单和举报记录。管理员可以举报垃圾消息、拦截指定来源 bot 的消息，也可以启用 Jev 模型检查新消息。

请先在测试群中使用。封禁可能删除历史消息，解除封禁无法恢复已删除的内容。

## 1. 创建 Telegram bot

1. 向 [@BotFather](https://t.me/BotFather) 发送 `/newbot`，按提示创建 bot，保存 token 和用户名。
2. 在 BotFather 中关闭 Group Privacy Mode（群组隐私模式）。需要接收其他 bot 的消息时，也启用 [Bot-to-Bot Communication Mode](https://core.telegram.org/api/bots/bot-to-bot)。
3. 将 bot 加入群组，设为管理员，授予「删除消息」和「封禁用户」权限。管理频道评论时，将 bot 加入频道关联的讨论群。

封禁和禁言适用于超级群，包括频道讨论群；普通群只支持删除消息。

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

将以下设置保存为 Worker secret（运行时密钥）。每条 `secret put` 命令都会提示输入对应的值：

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

举报时，选择目标消息的「回复」，输入 `@`，从 Telegram 候选列表中选择管理 bot，再发送。发送前核对回复目标。

| 操作 | 超级群中的处理结果 |
| --- | --- |
| 已授权的普通成员举报 | 保存证据，保留目标消息和举报消息。 |
| 已授权的管理员或聊天身份举报 | 删除目标消息，封禁非管理员发送者，清理符合条件的索引历史；清理成功后删除举报消息。 |
| 用户通过被拦截的来源 bot 发消息 | 删除匹配消息，永久禁言非管理员发送者，保留其他历史消息和成员身份。 |
| 账号黑名单中的用户发消息 | 对尚未封禁的账号执行封禁，清理符合条件的索引消息。 |

群主和管理员免于禁言、封禁，但被举报或匹配来源规则的消息仍可能被删除。账号黑名单匹配不会删除管理员的消息。

历史清理只覆盖 bot 在同一群中收到的消息，范围截至触发消息或在举报之前，且距今不足 48 小时。bot 无法搜索未收到的历史。Telegram 可能在封禁时删除更多历史消息。管理员授权的举报确认封禁成功后，实际发送者进入 `blacklisted_users`，由同一个 bot 跨群共用。举报不会将代发消息的来源 bot 加入来源名单。

### 添加来源

用已授权身份在私聊或群聊中发送 `/bs @example_bot`。群内可以使用 `/bs@管理bot用户名 @example_bot` 明确指定接收命令的 bot。请替换示例用户名。bot 将解析出的 ID 保存到 `blacklisted_sources`，并回复该数字 ID。

命令只添加来源，不删除消息、不改变成员状态，也不写入账号黑名单。后续消息中的内联 bot（`via_bot.id`）或转发来源 bot 命中名单时，按上表中的来源过滤策略处理。

用户名解析取决于 Telegram，部分账号无法查询；查询失败不会添加条目。命令不要求目标必须是 bot。来源匹配只适用于 Telegram 明确标注了 bot 来源的内联消息或转发消息；添加普通用户或群组 ID 不会拦截其直接发送的消息。复制粘贴的内容和隐藏来源的转发不匹配此规则。

### 查看名单和解除误报

在 Cloudflare 控制台打开 **D1 → anti-fwd-spam-reports → Console**，查询名单：

```sql
SELECT source_id FROM blacklisted_sources ORDER BY source_id;
SELECT bot_id, user_id FROM blacklisted_users ORDER BY bot_id, user_id;
```

删除条目时，从查询结果复制对应 ID，替换以下示例。只执行需要修正的名单对应的语句：

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

bot 在其他管理规则之后，用 Jev 检查符合条件的用户新消息。垃圾消息分数达到 0.95 时，只删除该条消息，不禁言、不封禁、不添加黑名单。分数是估计值，不代表准确率。编辑消息、服务事件、bot 消息及以群组或频道身份发送的消息不参与检查。

临时失败时，bot 保留消息，按 1、2、5 分钟安排重试。请保留定时任务，积压可能延迟重试。每次请求都可能产生费用，也可能向另一个已配置平台发送相同内容。

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

D1 保留完整举报 JSON（包含被举报消息）、来源命令及其解析出的 ID，保留期为 3 天。临时处理记录也在 3 天后到期。bot 为近期历史清理建立消息 ID 和时间索引，索引不保存正文。模型任务暂存提交给模型的内容。bot 不下载媒体文件。

来源和账号黑名单长期保留，直到手动删除。定时任务清理到期记录，服务故障或额度耗尽可能延迟清理。Cloudflare 备份另有[保留规则](https://developers.cloudflare.com/d1/reference/time-travel/)。查看或导出数据库时，不要公开成员信息。
