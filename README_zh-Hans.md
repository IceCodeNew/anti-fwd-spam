# anti-fwd-spam

[English](README.md) | 简体中文

群管理员可以将这个 Telegram bot 部署到自己的 Cloudflare 账户，自动删除指定来源 bot 的消息，并处理垃圾消息举报。

## 创建 bot 并加入群组

1. 在 [@BotFather](https://t.me/BotFather) 中发送 `/newbot`，按提示创建 bot，保存 token 和用户名。
2. 在 BotFather 中为该 bot 关闭 Group Privacy Mode；需要接收其他 bot 的消息时，按 [Telegram 的设置说明](https://core.telegram.org/api/bots/bot-to-bot)启用 Bot-to-Bot Communication Mode。
3. 将 bot 加入需要管理的群组，设为管理员，并授予「删除消息」和「封禁用户」权限。
4. 对于频道评论区，将 bot 加入频道关联的讨论群。禁言和举报封禁适用于超级群，包括频道讨论群；普通群只执行删除。

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

希望避免付费时，保持 Cloudflare Free 计划，并在控制台查看用量。部署前查看 [Workers](https://developers.cloudflare.com/workers/platform/pricing/) 和 [D1](https://developers.cloudflare.com/d1/platform/pricing/) 官方价格页，确认当前额度。

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

将命令返回的 `database_id` 填入 `wrangler.jsonc` 的 `d1_databases`，替换仓库中的 ID。保留 `binding` 为 `REPORTS`，然后执行数据库迁移：

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

确认返回 JSON 中的 `ok` 和 `result` 都为 `true`。后续更新规则或代码时，先用[数据库迁移命令](#配置消息来源和数据库)执行待处理的迁移，再运行 `mise exec -- uv run pywrangler deploy`。只有地址或密钥变更时才需要再次注册 webhook。

## 举报垃圾消息

1. 在群里长按或右键目标消息，选择「回复」。
2. 输入 `@`，从 Telegram 的候选列表中选择已部署的管理 bot，然后发送。

普通成员举报时，bot 只保存证据，目标消息和举报消息都保留在群内。群主或管理员举报时，bot 会永久封禁超级群中的非管理员发送者，阻止其评论和再次入群，直到管理员解除封禁。bot 删除目标消息，并清理该发送者在同一群中已建立索引的消息，范围限于举报之前发送、发送时间距今不足 48 小时的消息。每次投递最多处理 100 条索引消息，剩余部分随重试继续清理。举报前应核对回复目标：解除封禁无法恢复已删除的消息。

索引只覆盖启用后 bot 实际收到的消息，bot 无法搜索更早的历史或补回漏收的更新。bot 仍会通过 Telegram 的封禁接口请求清理历史，但无法确认索引范围外的清理结果。管理员应检查是否有残留消息，必要时手动删除。

bot 确认目标消息已删除或不存在，并完成符合条件的历史清理后，才会清理管理员的举报消息。如果 Telegram 拒绝删除目标，bot 会保留举报；封禁成功时，日志会包含 `deletion rejected; banned`，符合条件的近期历史仍会处理。`history cleanup rejected` 表示 Telegram 拒绝了批量删除请求，管理员应手动检查残留消息。匿名管理员可以用群组本身的身份举报；bot 不处理以关联频道或其他聊天身份发送的举报。

bot 保存封禁成功的记录后，遇到临时删除失败时，会在重试中继续删除消息，不会重复封禁。如果日志出现 `ban confirmation failed`，表示 bot 无法确认结果，不会因同一条举报再次封禁。管理员应检查发送者的成员状态；如果仍需处理，可以发送新的举报，或在 Telegram 的成员设置中手动封禁。bot 会保留未确认的举报消息，供管理员检查。

自动匹配时，bot 只删除匹配到的消息，并永久禁言发送者，保留其历史消息和群成员身份。目标发送者为群主、管理员，或以群组、频道身份发言时，bot 只删除目标消息，不执行禁言或封禁。

自动拦截只适用于 Telegram 保留了指定 bot 来源的内联消息或转发消息。复制粘贴的内容、隐藏来源的转发需要手动举报。

## 在测试群验证

在测试超级群或频道讨论群中，使用一个非管理员测试账号，先发送一条普通消息，再通过黑名单中的来源 bot 发送一条内联消息。确认只有后一条被删除，测试账号无法继续发言，前一条消息仍在。

接着在群管理的成员权限设置中解除该账号的禁言，在 bot 运行期间发送几条消息和一张贴纸。先用普通成员账号举报贴纸，确认消息保留；再用管理员账号举报，确认目标消息、此前已建立索引的消息和举报消息都已删除，发送者无法通过关联频道继续评论，也无法通过邀请链接重新入群。其他用户的消息和其他群中的消息应保留。

启用「保持匿名」（Remain Anonymous），以群组本身的身份再次执行管理员举报。也用尚未加入讨论群的评论者账号测试，检查目标消息是否删除，以及该账号能否继续评论或加入群组。

测试管理员保护时，举报一条管理员发送的消息。确认只有目标消息被删除，该管理员仍能发言，其他历史消息保留。每轮测试后手动解除测试账号的禁言或封禁。涉及删除历史消息的测试应使用专门的测试账号和可丢弃的测试群。

## 检查 webhook 和处理失败

在仍保存 `BOT_TOKEN` 的终端运行；换了终端时，先重新执行上面的 `read -rsp` token 输入命令。

```bash
curl --silent --show-error --fail-with-body \
  "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

检查 `result.url` 是否等于发布地址加 `/webhook`。发送测试消息后再次查询：`pending_update_count` 持续增加或出现新的 `last_error_message` 时，打开 Cloudflare 控制台中的该 Worker，查看日志。

- 遇到 `401`，重新保存 webhook 密钥，并用同一个值执行「注册 webhook」。
- 遇到 `404`，核对地址是否以 `/webhook` 结尾。
- 删除、禁言或封禁失败时，核对 bot 的管理员权限，以及目标发送者是否为管理员或匿名身份。`report cleanup rejected` 表示举报消息删除被拒绝，应从同一条日志中查看目标消息的处理结果。对于 `report cleanup pending` 响应，Telegram 会重试投递。
- 举报存储或消息索引失败时，在 Cloudflare 控制台检查 `REPORTS` 绑定、数据库迁移和 D1 用量。`history cleanup pending retry` 表示还有批次待处理，或临时错误中断了清理；bot 会保留举报消息，等待后续处理。

`pending_update_count` 为 `0` 仅表示没有积压，仍需在测试群确认消息和成员状态的变化。处理完后运行 `unset BOT_TOKEN`，清除当前终端变量。

## 查看存储记录

在 Cloudflare 控制台打开 D1 数据库 `anti-fwd-spam-reports`，查看 `reports` 表。`raw_update` 保存 Telegram 投递的完整举报 JSON，包含被回复的消息；`classification` 保存消息类型和媒体字段分类。查看或导出时，避免公开成员信息。

举报记录保留 3 天，不下载图片或其他媒体文件。定时任务清理到期记录；服务故障或额度耗尽可能延迟删除。Cloudflare 的备份另有保留周期，具体见 [D1 Time Travel](https://developers.cloudflare.com/d1/reference/time-travel/)。

`recent_messages` 表只保存超级群中已收到的用户消息对应的 bot、群、发送者和消息 ID，以及原始发送时间，不保存正文或媒体。编辑消息不会延长保留时间。消息发送满 48 小时后，bot 不再选取它进行删除；定时任务分批清理到期索引，积压可能延迟清理。批量删除成功后会提前移除索引。建立索引会增加 D1 写入开销，即使没有举报，也应关注数据库用量。
