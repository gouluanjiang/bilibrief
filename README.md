# BiliBrief-GHA

一个**不需要 24 小时在线服务器**的 Bilibili 关注更新采集器。

它利用 GitHub Actions 定时启动临时云端环境，读取你 Bilibili 登录账号的“关注动态时间线”，保存最近几天抓到的更新，然后通过 GitHub Pages 发布成只读 JSON，供 ChatGPT 的每日简报读取。

> 适合你的场景：监控一批关注的 UP 主，收集“新视频 + 动态”，最后由 ChatGPT 再过滤抽奖、水贴、重复宣传等低价值内容。

## 架构

```text
Bilibili 监控账号
   ↓  Cookie 只存 GitHub Secrets
GitHub Actions（每天 4 次）
   ↓
Bilibili 关注动态 Feed
   ↓
Rolling Archive（仓库 data/archive.json）
   ↓
GitHub Pages
   ├─ latest.json  最近约 30 小时
   ├─ recent.json  最近约 72 小时
   └─ health.json  采集健康状态
   ↓
ChatGPT 每日简报：筛掉抽奖/水贴/重复宣传，再总结
```

## 重要安全原则

1. **强烈建议使用 Bilibili 监控小号**，不要用主账号。
2. 不要把账号密码、Cookie、SESSDATA 发给 ChatGPT 或任何人。
3. Cookie 只放在 GitHub 仓库的 `Settings → Secrets and variables → Actions → Repository secrets` 中。
4. GitHub Pages 输出的 JSON **绝不包含 Cookie**。
5. 如果使用监控小号，让它只关注你希望进入日报的账号；这样 Feed 天然就是监控名单，无需维护 122 个 UID。

## 1. 创建 GitHub 仓库

在 GitHub 新建一个仓库，例如：

```text
bilibrief
```

建议先用 **Public**，因为 ChatGPT 需要通过网页读取最终 JSON。仓库公开不等于 Cookie 公开：Cookie 存在 GitHub Secrets，不会写入代码。

把本项目里的所有文件上传到仓库根目录，目录结构应类似：

```text
bilibrief/
├─ .github/
│  └─ workflows/
│     └─ collect.yml
├─ data/
│  └─ archive.json
├─ public/
│  ├─ index.html
│  ├─ latest.json
│  ├─ recent.json
│  └─ health.json
├─ src/
│  └─ collector.py
├─ tests/
│  └─ test_parser.py
├─ .gitignore
├─ LICENSE
├─ README.md
└─ requirements.txt
```

## 2. 获取 Cookie（只在你自己的电脑上操作）

推荐监控小号。

1. 在电脑浏览器登录 Bilibili。
2. 打开 Bilibili 动态页，例如 `https://t.bilibili.com/`。
3. 按 `F12` → `Network / 网络`。
4. 刷新页面。
5. 找一个访问 `api.bilibili.com` 且属于动态 Feed 的请求，例如 URL 中包含：

   ```text
   /x/polymer/web-dynamic/v1/feed/all
   ```

6. 点击该请求，在 **Request Headers / 请求标头** 找 `Cookie:`。
7. 复制 `Cookie:` 后面的整段值。

**不要把这段 Cookie 发到聊天里。**

如果你不方便找到 Feed 请求，也可以打开 Network 后刷新动态页，再在过滤框输入 `feed/all`。

## 3. 把 Cookie 放进 GitHub Secret

仓库页面：

`Settings → Secrets and variables → Actions → New repository secret`

创建：

```text
Name: BILIBILI_COOKIE
Secret: （刚才复制的整段 Cookie）
```

保存。

可选：如果未来 GitHub 云端 IP 被 Bilibili 拒绝，而你自己有可靠 HTTP(S) 代理，可再创建：

```text
HTTP_PROXY_URL
```

例如 `http://user:pass@host:port`。**没有代理就不要填。**

## 4. 开启 GitHub Pages

进入：

`Settings → Pages`

在 **Build and deployment** 中把 Source 设置为：

```text
GitHub Actions
```

保存。

## 5. 第一次手动运行

进入仓库：

`Actions → Collect Bilibili updates → Run workflow`

等几分钟。

成功后日志中会看到类似：

```text
OK: fetched=84, latest=27, archive=84
```

然后在 `Settings → Pages` 或 Actions 的 deploy job 中能看到你的 Pages 地址，例如：

```text
https://你的GitHub用户名.github.io/bilibrief/
```

打开：

```text
https://你的GitHub用户名.github.io/bilibrief/health.json
```

正常应看到：

```json
{
  "status": "ok",
  "checked_at": "...",
  "last_success_at": "..."
}
```

再打开：

```text
https://你的GitHub用户名.github.io/bilibrief/latest.json
```

里面就是最近约 30 小时捕获到的更新。

## 6. 自动运行时间

`.github/workflows/collect.yml` 默认每天运行约 4 次（中国标准时间 UTC+8）：

- 01:20
- 07:20
- 13:20
- 19:20

用户已确认日报时间为每天北京时间 21:00（Asia/Shanghai）；按现有采集计划，理论上此前有一次 19:20 左右的采集。日报任务由主任务单独设置，本仓库不会创建或启用推送。

注意：GitHub 官方的 scheduled workflow **不是实时调度系统**，高峰期可能延迟几分钟甚至更久，所以 `latest.json` 使用 30 小时窗口，而不是死卡 24 小时。

## 7. 如何接入 ChatGPT 日报

当 Pages 跑通后，把你的 `latest.json` 地址发给 ChatGPT，例如：

```text
https://example.github.io/bilibrief/latest.json
```

日报规则建议写成：

```text
每天生成日报时，读取这个 BiliBrief latest.json。
只处理 published_at 落在最近约 24 小时内的项目。

视频：原则上保留正式投稿，但过滤明显的纯搬运、重复切片（如果对我无价值）。
动态：只保留有实质信息的内容，如新作公布、发售/测试日期、版本更新、开发进度、重要公告、重大合作、创作者重要近况等。

剔除抽奖/转发抽奖、求赞求关注、纯表情包、无意义日常、节日祝福、重复宣传、纯开播提醒等。
如果视频与宣传动态指向同一内容，优先保留视频，动态仅在补充额外信息时保留。
```

## JSON 字段

单条内容大致如下：

```json
{
  "id": "动态ID",
  "kind": "video | dynamic | repost | article",
  "up": "UP主昵称",
  "up_uid": "UID",
  "published_at": "2026-09-05T08:30:00Z",
  "title": "标题",
  "text": "动态正文或简介",
  "url": "原始 Bilibili 链接",
  "first_seen_at": "第一次抓到的时间",
  "last_seen_at": "最近一次仍抓到的时间"
}
```

`latest.json` 默认保留最近约 30 小时；`recent.json` 是 72 小时兜底；仓库内部 `data/archive.json` 保存 7 天，这样某条内容在被抓到后即使后来从 Feed 中消失，也能在保留期内继续存在于归档中。

## 常见问题

### `health.json` 显示 Cookie expired / login verification failed

这表示登录校验未通过，不能仅据此断定 Cookie 已过期。由账号所有者在自己的浏览器确认监控账号能正常登录、访问关注动态，并在仓库设置中私下覆盖 `BILIBILI_COOKIE`。不要将凭据发到聊天、PR 或仓库文件。然后由维护者在审核合并修复后，从默认分支手动运行一次 workflow，并同时检查采集、部署结果和公开 JSON 的时间。

### 失败状态、陈旧数据与恢复

- `health.json` 记录本次 `checked_at`、最近成功的 `last_success_at`、固定分类 `error_code` 和 `coverage.complete`。失败时保留 archive/latest/recent 的字节和原生成时间；缺少 Cookie 也不会丢失历史成功时间。
- 所有输出先暂存，再替换。发生替换失败时回滚；回滚或健康状态写入失败会阻止本次发布。进程被强制终止、依赖安装或发布失败仍可能让公开状态变旧，消费者必须独立检查新鲜度。
- 采集失败但错误状态可安全保存时，工作流仍尝试保存和发布健康状态，采集 job 和整个 run 保持失败。Pages 是否实际发布以单独 `deploy` job 为准，不能从 `health.status` 推断部署成功。
- 默认分支之外不会读取生产采集 Secrets、提交生产数据或部署。PR CI 仅运行离线测试和工作流校验。
- 缺少分页字段、重复游标、空页却声称还有下一页、畸形条目或达到 30 页上限均报 `incomplete_feed`，保留旧快照。`coverage.complete=true` 仅指观察到的关注流窗口正常结束，不保证上游未返回、删除或超出保留期的内容。
- `new_items` 是本次新增到保留范围的可发布动态数；它不是已送达日报数。重复抓取不会重复追加同一动态。

稳定读取入口是 `https://gouluanjiang.github.io/bilibrief/health.json`、`latest.json` 和 `recent.json`。每次先校验健康状态与三个时间；超过 10 小时未成功、未来时间、不一致快照或读取失败都应报告“数据源异常”，不能说“无更新”。这三个路径继续兼容原有调用。

### 日报候选接口与去重记录

见 [独立日报接续说明](docs/chatgpt-daily-task.md)。`src/brief.py` 是只读离线接口，不发送通知、不更改去重记录：

```bash
python src/brief.py --since 2026-09-30T13:00:00Z --until 2026-10-01T13:00:00Z --reported /private/path/reported.json
```

时间必须带时区，主窗口为 `(since, until]`。结果按 UP 主分组，包含标题、发布时间、来自正文/简介的候选摘要、原链接、`identity_keys` 和 `catch_up`。`source_error` 返回非零退出码；只有新鲜、完整且可校验的快照才返回 `updates` 或 `no_updates`。内容筛选、精简摘要和送达确认仍由日报任务负责。

`reported.json` 是此前**实际送达条目**的稳定标识字符串数组，保存在任务自己的持久存储，不提交仓库。标识包括动态 ID、BV 号、去掉跟踪参数的原链接。只有实际发送成功后才合并那些已发送条目的标识；预览、发送失败、采集异常都不能推进记录。第一次没有记录时仅取主窗口；有记录后可从 72 小时 `recent` 中补漏。视频和同目标宣传合并，额外正文保留为 `related_updates` 供语义筛选；不可把它当成已观看视频的摘要。

### GitHub Actions 日志出现 412 / 403 / connection reset

这通常是 Bilibili 风控或云端 IP 问题，不代表账号被封。

项目已经自带：

- 浏览器 User-Agent
- Referer
- 每页请求间隔
- 指数退避重试

如果仍频繁失败：

1. 优先换监控小号 Cookie；
2. 降低运行频率；
3. 如果你有自己的可靠代理，再设置 `HTTP_PROXY_URL`；
4. 不建议为了稳定性购买来源不明的代理服务。

### 为什么不直接遍历 122 个 UP 的主页？

关注 Feed 一次就能覆盖你的关注流，请求数更少，也会自动跟随“关注/取关”变化。逐个扫 122 个主页反而更容易触发风控。

### 为什么还要保存 archive？

如果一条动态在下午发布、晚上删除，但 19:20 的任务已经抓到，它仍会在 rolling archive 中保留一段时间，因此第二天的日报有机会看到它。

### 会不会把直播刷屏？

纯直播状态卡（`DYNAMIC_TYPE_LIVE*` / `MAJOR_TYPE_LIVE*`）在发布 JSON 前会被去掉。真正写成普通动态的“重要直播预告”仍会保留，之后交给 ChatGPT 判断是否值得放进日报。

## 技术说明

采集器使用 Bilibili Web 动态 Feed：

```text
GET https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all
```

这是 Bilibili 网页自身使用的接口，属于未承诺长期稳定的 Web API。因此 Bilibili 改版后字段或风控策略可能变化；项目采用了尽量防御性的解析方式，但仍可能需要更新。

GitHub Actions 使用 GitHub-hosted runner，定时任务可能有调度延迟；GitHub Pages 用于发布静态 JSON。

## 本地语法测试（可选）

```bash
python -m pip install -r requirements.txt
python -m pytest tests -q
```

如果你想本地真实抓取，临时设置环境变量后运行：

```bash
BILIBILI_COOKIE='你的cookie' python src/collector.py
```

Windows PowerShell：

```powershell
$env:BILIBILI_COOKIE='你的cookie'
python src/collector.py
```

注意：本地测试结束后不要把 Cookie 写进任何文件或提交到 GitHub。
