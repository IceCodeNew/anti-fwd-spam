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

来源 ID 保存在 D1 的 `blacklisted_sources` 表中。

创建举报数据库：

```bash
mise exec -- uv run pywrangler d1 create anti-fwd-spam-reports
```

将命令返回的 `database_id` 填入 `wrangler.jsonc` 的 `d1_databases`，替换仓库中的 ID。保留 `binding` 为 `REPORTS`，然后执行数据库迁移：

```bash
mise exec -- uv run pywrangler d1 migrations apply anti-fwd-spam-reports --remote
```

已有部署时，继续使用原数据库 ID，跳过数据库创建命令。部署代码前先执行迁移。新数据库的来源名单为空，使用 `/ban` 添加来源。添加的条目不会因部署或证据过期而丢失。

从使用 `BLACKLIST_BOT_IDS` 的版本升级时，先保存旧配置中的 ID。执行数据库迁移后、部署前，将这些 ID 逐个导入 D1。把下方示例 ID 替换为已有的来源 ID，并对剩余 ID 重复执行：

```bash
mise exec -- uv run pywrangler d1 execute anti-fwd-spam-reports --remote --command 'INSERT OR IGNORE INTO blacklisted_sources (source_id) VALUES (123456789);'
mise exec -- uv run pywrangler d1 execute anti-fwd-spam-reports --remote --command 'SELECT source_id FROM blacklisted_sources ORDER BY source_id;'
```

确认查询结果包含旧名单中的每个 ID 后再部署。Worker 不再读取 `BLACKLIST_BOT_IDS`。

### 保存凭据并发布

将管理 bot 的用户名保存为运行时 secret，不含 `@`。改名后，用同一命令更新：

```bash
mise exec -- uv run pywrangler secret put BOT_USERNAME
```

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

确认返回 JSON 中的 `ok` 和 `result` 都为 `true`。后续更新规则或代码时，先用[数据库迁移命令](#配置消息来源和数据库)执行待处理的迁移，再运行 `mise exec -- uv run pywrangler deploy`。地址、密钥或 `allowed_updates` 变更时，需要再次注册 webhook。

## 举报垃圾消息

管理员先将允许举报的身份配置为 Worker 运行时 secret：

```bash
mise exec -- uv run pywrangler secret put REPORTER_IDS
```

`REPORTER_IDS` 同时填写用户和发言所用的聊天身份数字 ID，多个值用英文逗号分隔。以聊天身份发送的消息使用 `sender_chat.id` 匹配，否则使用真实举报人的 `from.id`；命中列表后，bot 即受理举报。发言身份不必与目标群相同。将聊天身份加入列表不会授权以个人账号举报的群成员，应将这些用户 ID 也加入同一列表。对于以聊天身份发送的消息，bot 不使用 Telegram 提供的兼容 `from` 用户授权。

列表为空或未配置时，bot 不受理举报。未获授权的举报人即使是群主，也不能保存举报证据、添加黑名单或触发举报删除与处罚。移除身份后，该身份尚未完成的举报重试也会停止。自动拦截和已有账号黑名单处理继续运行。

1. 在群里长按或右键目标消息，选择「回复」。
2. 输入 `@`，从 Telegram 的候选列表中选择已部署的管理 bot，然后发送。

普通成员举报时，bot 只保存证据，目标消息和举报消息都保留在群内。群主或管理员举报时，bot 会永久封禁超级群中的非管理员发送者，阻止其评论和再次入群，直到管理员解除封禁。bot 删除目标消息，并清理该发送者在同一群中已建立索引的消息，范围限于举报之前发送、发送时间距今不足 48 小时的消息。每次投递最多处理 100 条索引消息，剩余部分随重试继续清理。举报前应核对回复目标：解除封禁无法恢复已删除的消息。

索引只覆盖启用后 bot 实际收到的消息，bot 无法搜索更早的历史或补回漏收的更新。Telegram 的超级群封禁接口会强制启用历史撤销，即使请求没有传入该参数；bot 无法确认索引范围外的清理结果。管理员应检查是否有残留消息，必要时手动删除。

bot 确认目标消息已删除或不存在，并完成符合条件的历史清理后，才会清理管理员的举报消息。如果 Telegram 拒绝删除目标，bot 会保留举报；封禁成功时，日志会包含 `deletion rejected; banned`，符合条件的近期历史仍会处理。`history cleanup rejected` 表示 Telegram 拒绝了批量删除请求，管理员应手动检查残留消息。获准的聊天身份（包括匿名管理员）提交举报时，bot 按管理员举报处理。

bot 保存封禁成功的记录后，遇到临时删除失败时，会在重试中继续删除消息，不会重复封禁。如果日志出现 `ban confirmation failed`，表示 bot 无法确认结果，不会因同一条举报再次封禁。管理员应检查发送者的成员状态；如果仍需处理，可以发送新的举报，或在 Telegram 的成员设置中手动封禁。bot 会保留未确认的举报消息，供管理员检查。

匹配来源 bot 时，bot 只删除匹配到的消息，并永久禁言发送者，保留其历史消息和群成员身份。目标发送者为群主、管理员，或以群组、频道身份发言时，bot 只删除目标消息，不执行禁言或封禁。

来源 bot 拦截只适用于 Telegram 保留了指定 bot 来源的内联消息或转发消息。复制粘贴的内容、隐藏来源的转发可以手动举报，或启用下方的可选模型检查。

## 通过用户名添加来源

使用 `REPORTER_IDS` 中的身份发送 `/ban example_bot` 或 `/ban @example_bot`。在群内可以使用 `/ban@管理bot用户名 example_bot` 指定接收命令的 bot。bot 会回复解析出的数字 ID 和处理结果，不要求目标账号必须是 bot。

用户名查询取决于 Telegram 的 `getChat` 返回结果，不能保证任意普通用户的用户名都可查询。Telegram 返回不可重试的查询错误时，bot 会回复无法解析用户名，来源名单保持不变。

在超级群中，命令还会封禁解析出的用户账号，并清理截至命令消息时已索引、发送时间距今不足 48 小时的历史消息。群主和管理员仍受保护。已封禁的账号保持封禁，命令不会解封，也不会通过禁言替换封禁状态。确认封禁成功后，bot 还会将账号写入账号黑名单。封禁结果不确定时，管理员应先检查成员状态，再决定是否发送新命令。

在私聊或普通群中，命令只添加来源 ID。解析结果为群组或频道 ID 时，bot 会保存来源，但不会将它传给用户封禁接口。现有来源匹配规则不变：添加普通用户或聊天 ID 不会使其直接发送的消息命中来源过滤。

来源名单不会过期。使用以下命令查看：

```bash
mise exec -- uv run pywrangler d1 execute anti-fwd-spam-reports --remote --command 'SELECT source_id FROM blacklisted_sources ORDER BY source_id;'
```

bot 将命令证据和解析出的 ID 保留 3 天。用户名变更后，同一命令重投时仍使用保存的 ID。编辑命令或使用未授权身份不会添加条目。回复的投递结果无法确认时，Telegram 重投可能导致重复回复。

## 启用模型垃圾消息检测

将所用平台的 API key 保存为 **Worker secret**，然后部署代码。bot 按下表顺序选取配置了非空密钥的平台，无需额外指定平台：

| 平台 | Worker secret |
| --- | --- |
| [TypeSafe AI](https://ai-sdk.dev/providers/ai-sdk-providers/typesafe-ai) | `TYPESAFE_AI_API_KEY` |
| [Vercel AI Gateway](https://vercel.com/ai-gateway/models/jev) | `AI_GATEWAY_API_KEY` |
| [Experiential](https://platform.experientiallabs.ai/models/jev-latest) | `EXPERIENTIAL_API_KEY` |
| [OpenCode Zen](https://opencode.ai/docs/zen/) | `OPENCODE_API_KEY` |

例如，使用 Vercel 时执行：

```bash
mise exec -- uv run pywrangler secret put AI_GATEWAY_API_KEY
mise exec -- uv run pywrangler deploy
```

OpenCode 使用 Zen 接口，不使用 Go。管理员应核对各平台的账户验证和计费要求。接口地址和模型 ID 定义在 [`src/anti_fwd_spam/model.py`](src/anti_fwd_spam/model.py) 的 `MODEL_PROVIDERS` 中。

bot 会将符合条件的群内新消息发送给 Jev，结合发送者昵称、可获取的个人简介、正文或媒体说明，以及格式信息和选定的媒体描述进行判断。配置多个密钥后，bot 可能在重试时向多个平台发送同一份内容，管理员应在启用前查看各平台的隐私条款和价格。bot 不发送被回复消息、聊天历史或媒体文件 ID，也不下载或识别媒体内容。简介无法获取时，bot 将其标记为未知；查询成功但未返回简介时，标记为空。

返回的垃圾消息概率达到 0.95 时，bot 只删除当前消息。该分数是模型估计值，不代表准确率达到 95%。模型判断不会触发禁言、封禁、加入黑名单或清理历史。bot 优先处理已有的账号黑名单、来源 bot 规则和举报。编辑消息、服务消息、bot 发送的消息，以及频道身份或匿名管理员发送的消息不参与模型检查。

简介查询和模型推理各有 8 秒超时限制；简介查询失败时，bot 仍可继续分类。遇到网络错误、超时，以及 HTTP 408、429 或 5xx 响应时，bot 保留消息，依次等待 1、2、5 分钟后重试，并轮换已配置的平台，必要时从头循环。各平台合计最多尝试四次。遇到永久 HTTP 拒绝时，只有首轮中还有未尝试的平台，bot 才会继续安排重试。遇到无效答案或低于阈值的有效分数时，bot 停止处理，不再询问其他平台。每次推理都可能产生 API 费用。分类成功后，删除遇到临时失败时只重试删除，不再请求模型。

管理员应保持 Worker 的 Cron Trigger 启用。定时任务每分钟处理一条到期消息，积压可能延迟重试。消息发送满 48 小时后，bot 停止处理。收到编辑更新时，bot 取消原内容的待处理任务；已经发送给 Telegram 的删除请求无法撤回。待处理推理使用执行时配置的密钥；更改密钥可能跳过或重复尝试某个平台，但不会重置尝试次数。希望保持轮换顺序时，应在待处理任务结束前保留相同的平台密钥集合。删除已配置的全部模型 secret 可关闭新检查并暂停待处理任务，包括删除任务。仅使用 Vercel 时执行：

```bash
mise exec -- uv run pywrangler secret delete AI_GATEWAY_API_KEY
```

管理员应先在可丢弃的测试群中发送普通讨论、推广消息和引用诈骗内容的警示消息，检查删除后是否保留群成员身份和之前的消息。同时在 Cloudflare 中查看 Worker CPU 用量和错误。外部网络等待不计入 CPU 时间，但本地执行仍受套餐 CPU 额度约束。开发终端或 Amp project 中的密钥不会自动成为已部署 Worker 的 secret。

## 跨群封禁已举报账号

Telegram 确认管理员举报触发的封禁成功后，bot 会将账号加入数据库黑名单。普通成员举报、自动禁言和未确认的封禁不会加入账号。管理员只应授权可信的[举报身份](#举报垃圾消息)，其确认封禁会影响同一个 bot 管理的其他群组。

bot 对超级群内真实用户发送的每条新消息查询账号黑名单。命中时，bot 优先执行账号封禁策略：尚未封禁的账号会被封禁，随后 bot 只批量清理截至触发消息、符合条件的索引消息。触发消息符合索引条件时也会进入索引。bot 不额外尝试单条删除或搜索历史。群主和管理员保留其成员身份和消息。普通群和编辑消息不会触发账号黑名单处理。每次检查增加一次 D1 查询，应关注数据库用量。

黑名单账号发送新消息时，bot 才执行处理。加入群组或提升 bot 权限不会提前封禁用户。Telegram 可能在超级群封禁时撤销其他历史消息，其 API 没有关闭此副作用的选项。

三天的处理记录保留期间，重复投递不会重复已确认的封禁。但账号仍在黑名单中时，即使管理员手动解封，下一条新消息也会再次触发封禁。处理误报时，应同时移除数据库黑名单记录并在 Telegram 中解封。日志出现 `ban confirmation failed` 时，管理员应手动核对成员状态；结果不明的请求不会因重投再次封禁。明确的临时拒绝仍可重试。遇到 `history cleanup rejected`，管理员应手动检查残留的索引消息。

## 在测试群验证

在测试超级群或频道讨论群中，使用一个非管理员测试账号，先发送一条普通消息，再通过黑名单中的来源 bot 发送一条内联消息。确认只有后一条被删除，测试账号无法继续发言，前一条消息仍在。

管理员应先将测试举报人加入 `REPORTER_IDS`。接着在群管理的成员权限设置中解除该账号的禁言，在 bot 运行期间发送几条消息和一张贴纸。先用普通成员账号举报贴纸，确认消息保留；再用管理员账号举报，确认目标消息、此前已建立索引的消息和举报消息都已删除，发送者无法通过关联频道继续评论，也无法通过邀请链接重新入群。其他用户的消息和其他群中的消息应保留。

管理员将匿名发言所用的聊天身份 ID 加入 `REPORTER_IDS` 后，启用「保持匿名」（Remain Anonymous），再次执行管理员举报。确认未获授权的群主和聊天身份不能举报。也用尚未加入讨论群的评论者账号测试，检查目标消息是否删除，以及该账号能否继续评论或加入群组。

测试管理员保护时，举报一条管理员发送的消息。确认只有目标消息被删除，该管理员仍能发言，其他历史消息保留。每轮测试后手动解除测试账号的禁言或封禁。涉及删除历史消息的测试应使用专门的测试账号和可丢弃的测试群。

举报封禁成功后，将 bot 设为另一个可丢弃超级群的管理员。确认测试账号不会仅因入群而被封禁。用该账号发送新消息，检查消息是否被删除，以及账号是否无法再次入群。然后按下文说明从数据库黑名单中移除测试账号，在 Telegram 中解封，确认新发送的消息保留。

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
- 遇到 `mute retry skipped` 且仍需处理时，管理员应手动检查发送者权限。去重记录存在期间，bot 不会对同一条消息重复执行可能已经成功的禁言，编辑更新也受此限制，避免重投覆盖管理员解除禁言的操作。Telegram 明确拒绝的临时失败仍可重试；新的匹配消息可以再次触发禁言。
- 遇到 D1 存储错误，包括 `account moderation storage unavailable` 时，在 Cloudflare 控制台检查 `REPORTS` 绑定、数据库迁移和 D1 用量。禁言存储不可用时，自动拦截仍可删除目标，但禁言要等待存储恢复。`history cleanup pending retry` 表示还有批次待处理，或临时错误中断了清理；bot 会保留举报消息，等待后续处理。

`pending_update_count` 为 `0` 仅表示没有积压，仍需在测试群确认消息和成员状态的变化。处理完后运行 `unset BOT_TOKEN`，清除当前终端变量。

## 查看存储记录

在 Cloudflare 控制台打开 D1 数据库 `anti-fwd-spam-reports`，查看 `reports` 表。`raw_update` 保存 Telegram 投递的完整举报 JSON，包含被回复的消息；`classification` 保存消息类型和媒体字段分类。查看或导出时，避免公开成员信息。

举报记录保留 3 天。对于命中账号黑名单的新消息，bot 也在 `reports` 中保存完整更新 JSON 和处理进度，保留 3 天，以便重试删除而不重复已确认的封禁。bot 不下载图片或其他媒体文件。定时任务清理到期记录；服务故障或额度耗尽可能延迟删除。Cloudflare 的备份另有保留周期，具体见 [D1 Time Travel](https://developers.cloudflare.com/d1/reference/time-travel/)。

`recent_messages` 表只保存超级群中已收到的用户消息对应的 bot、群、发送者和消息 ID，以及原始发送时间，不保存正文或媒体。编辑消息不会延长保留时间。消息发送满 48 小时后，bot 不再选取它进行删除；定时任务分批清理到期索引，积压可能延迟清理。批量删除成功后会提前移除索引。建立索引会增加 D1 写入开销，即使没有举报，也应关注数据库用量。

bot 使用 `automatic_mutes` 表中的 bot、群和消息 ID 及到期时间，避免重复自动禁言，不保存消息内容。每条记录在创建后 3 天到期；bot 不会因重复投递或编辑更新刷新已有记录的到期时间。定时任务分批删除到期记录。

bot 在 `model_tasks` 表中暂存模型输入（昵称、可获取的简介和选定的消息内容）、标识符及处理进度。分类成功或处理终止时，bot 清空输入；收到编辑更新或耗尽重试次数也会清空输入。去重元数据在任务创建后 3 天到期，重试不会延长保留时间。消息发送满 48 小时后，定时任务也会清空未完成任务的内容。管理员可在 D1 控制台执行 `SELECT phase, COUNT(*) FROM model_tasks GROUP BY phase;`，查看任务状态且不展示成员内容。启用模型检查前应执行待处理的数据库迁移。

bot 在 `blacklisted_users` 表中长期保存 bot 和用户 ID，以及首次确认举报封禁的时间，直到人工移除。举报证据到期不会移除这些账号。

若要停止对误报账号执行后续黑名单匹配，在 D1 控制台执行以下 SQL，先替换两个数字 ID。然后在每个受影响群组的 Telegram 成员设置中解除封禁；只删数据库记录不会解除 Telegram 封禁。

```sql
DELETE FROM blacklisted_users WHERE bot_id = 123456789 AND user_id = 987654321;
```
