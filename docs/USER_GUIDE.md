# PhySc-agent 使用手册

> 版本 v0.1.0 · 更新 2026-08-16
> 只写现阶段已有功能；未实现的功能见文末"已知限制"，后续版本补齐后更新本手册。

---

## 1. 快速开始

```bash
cd phxsc-agent

# 1. 设置 DeepSeek API Key（必须）——两种方式任选：
#    方式 A：环境变量
export DEEPSEEK_API_KEY=sk-你的key
#    方式 B：项目根 .env 文件（推荐，KEY=VALUE 每行，# 注释）

# 2. （可选）语义记忆用智谱 embedding——不设也能用：
#    语义缓存自动降级为 exact-only（仅同 query 命中），记忆混合检索退化为 FTS5
export ZHIPU_API_KEY=你的智谱key

# 3. 启动（默认 investigate 模式）
.venv/bin/python -m phxsc.cli

# 指定模式启动 / 指定工作目录
.venv/bin/python -m phxsc.cli --mode plan
.venv/bin/python -m phxsc.cli --workdir /path/to/workdir   # 或设 PHXSC_WORKDIR
```

看到 `PhySc-agent 已启动` 即就绪。输入问题回车，agent 会自动调用工具（屏幕上显示 `→ 调用工具 xxx (0.8s)` 带耗时）。

**输入体验**（v0.0.2 起）：
- 输入 `/` 自动弹出命令补全列表，方向键选择，Enter 确认
- 左右方向键移动光标，上下方向键翻会话历史
- 提示符 `phxsc[investigate] > ` 受保护，退格不会删到它

## 2. 交互命令

| 命令 | 功能 |
|------|------|
| `/plan` `/investigate` `/typeset` | 切换模式（见第 3 节）；切换时自动保留最近 3 轮对话，话题不丢 |
| `/new` | 开启新会话（清空对话上下文；记忆/证据/闸门状态保留） |
| `/gate <问题>` | 请求级引用溯源校验（问题前加 /gate，本轮触发；其他轮不校验） |
| `/moa <问题>` | 多助手并行（MoA）回答同一问题，综合多路结果 |
| `/dedup [--file <路径>] [--rewrite] <文本>` | 查重 / 降重（支持文件或直接文本） |
| `/thinking` / `/thinking off` / `/thinking low` / `/thinking medium` / `/thinking high` | 思考力度：`low/medium/high`=三档力度（DeepSeek 映射 budget_tokens 2048/8192/32768，模型按需用；复杂推理质量随力度提升，代价是输出侧多花 reasoning token）；`off`=关闭（零思考零额外成本）；**默认 high**（首次启动即开最高力度）；无参=显示当前档位；`on`=medium 兼容别名；**档位持久化**到 `~/.phxsc/settings.json`——下次重启/重开会话自动恢复上次设置 |
| `/voice` / `/voice academic` / `/voice natural` | 文本风格：`academic` = 学术风（默认）；`natural` = 面向人读（口语化、短句、去空话套话和客套，输出前自检 AI 味）。轻量去味（去"总的来说/值得注意的是/总之"）默认对所有模式生效 |
| `/schedule list` | 列出定时任务 |
| `/schedule add "<cron>" <topic>` | 添加定时任务（cron 支持 `"每天 9:00"` 简写或标准五段 `"0 9 * * *"`，引号可省略） |
| `/schedule rm <id>` | 删除定时任务 |
| `/cache stats` | 查看缓存统计（exact / semantic / embed 三表 entries + hit_rate） |
| `/cache clear [semantic\|exact\|all]` | 清空指定缓存（需输入 y 确认） |
| `/skill list` | 列出所有可用技能（名称 + 描述 + 版本 + 路径；**已加载注入轨的显示 ★ 标记**） |
| `/skill loaded` | 列出注入轨中的技能（名称 + 字符数 + 总量；空轨提示） |
| `/skill load <name>` | 加载技能进注入轨（每轮全文注入，不截断；**总量超 8KB 时警告**；上限 8 个） |
| `/skill unload <name>` | 卸载已加载技能 |
| `/mcp list` / `/mcp status` | 查看 MCP servers 连接状态（已连接 server + 工具数 + 失败原因；未配置时提示） |
| `/sessions` | 列出历史会话（id/时间/模式/消息数/首条摘要，最近 20 个） |
| `/search <词>` | 跨会话全文检索历史消息（FTS5，≥3 字符） |
| `/resume <id>` | 恢复历史会话到当前上下文（清空当前 + 恢复原模式） |
| `/fork <id>` | 把历史会话消息并入当前上下文（不清空，继续对话） |
| `/model [provider/模型名 | 模型名]` | 查看当前 provider/模型；无斜杠=当前 provider 内切模型；含斜杠=跨 provider 切换（如 `/model zhipu/glm-4.5-air`）；切换持久化到 settings.json |
| `/provider [名称]` | 查看全部 provider（★=当前，含状态/默认模型）/ 切换 provider（如 `/provider zhipu`）；自定义 provider 见 ~/.phxsc/providers.json |
| `/stop` | 中断当前正在处理的任务（等当前 LLM 调用返回后生效；非忙碌时提示） |
| `/help` | 显示全部命令帮助 |
| `/exit` / `/quit` | 退出 |

**底栏状态栏**（常驻最底部，每次执行自动刷新）：
```
[investigate] │ reasoning effort:off │ ready │ deepseek-v4-flash │ 2k/1000000 [░░░░░] 0% │ 命中 85% │ 本轮 12.3s │ 总 3:45 │ 21:30
```
- `[模式]` 彩色标识（plan 青 / investigate 绿 / typeset 紫，PhySc 独有）
- `reasoning effort:` thinking 档位（off / low / medium / high，常显）
- 状态：`⚙ working`（执行中）/ `ready`（空闲）
- 上下文占用：当前对话粗估 token / 1M 窗口
- 服务端命中率：DeepSeek 前缀缓存命中率（有调用后显示，无数据自动隐藏）
- 本轮耗时 / 总运行时长 / 时钟

启动时显示当日 telemetry（调用次数/总 token/服务端缓存命中率/语义缓存命中率/估算成本）。

### 语义缓存与命中标记（v0.0.12）

- **三级缓存链**：`exact（字面同问）→ semantic（语义相近）→ LLM`。语义缓存只在字面未命中、闸门关闭、且问题不依赖上下文（无指代、够长）时查询。
- **⚡ 命中标记**：语义缓存命中时，回答下方会显示灰字 `⚡ 语义缓存命中 0.94 ← "原query"`——表示这次回答来自缓存（零 LLM 调用），`0.94` 是与历史问题的相似度（≥0.93 才命中）。
- **实体差异守卫**：如果两个问题长得像但涉及不同实体（如 Mn3Ga ↔ Mn3Sn、2024 ↔ 2025），守卫会强制 miss，宁可多调一次 LLM 也不让错误的缓存污染结果——所以看到某些"很像"的问题没命中是设计行为，不是 bug。
- **跨语言改写限制**：语义缓存对**同语言**改写命中稳定（0.96-0.99）；中英混写/对照的相似度约 0.8，低于阈值通常不命中（宁 miss 不污染）。
- **上下文依赖问题**（"它/这个/刚才/上一条"这类指代或太短的问题）自动跳过语义缓存，永远走 LLM，保证回答正确。
- **管理**：`/cache stats` 查看三表统计；`/cache clear semantic|exact|all` 确认式清空。
- **注意**：`/remember` 不是 CLI 命令（文档旧表述）——想记重要偏好直接自然语言说"记住：我的研究方向是……"，agent 会调 `remember` 工具写入。

### skill 体系（v0.0.13）

**什么是 skill**：一段 SKILL.md 格式的领域指令文件（frontmatter 写 name/description/version + 正文规范），让 agent 在特定任务下按固定规范干活——比如写综述时自动遵守 `review-writing` 的章节纪律、引论文时自动遵守 `citation-format` 的格式。格式对齐 agentskills.io 开放标准，**同一套 SKILL.md 也可以直接放进 Claude Code 的 `.claude/skills/` 或 Codex 的 `.codex/skills/` 使用**。

**skill 放哪（两级目录）**：

| 目录 | 说明 |
|------|------|
| 项目级 `skills/` | 随项目分发（进 git），所有人共享。当前 3 个：`citation-format` / `review-writing` / `three-part-reading` |
| 用户级 `~/.phxsc/skills/` | 个人私有。当前 1 个：`perovskite-domain`（你的钙钛矿方向领域卡）；`PHXSC_SKILLS` 环境变量可改用户级目录 |

> 同名冲突时用户级优先。想自己加 skill：建目录放 `SKILL.md`（frontmatter 必须有 `name`（小写字母开头）/`description`/`version`）即可，重启 CLI 自动识别。

**怎么用**：
- `/skill list` —— 查看全部可用技能（列表与加载状态无关，永远显示全部）
- `/skill load <name>` —— 把技能正文注入当前对话，之后每轮 agent 都遵守该规范（上限 8 个）
- `/skill unload <name>` —— 移除已加载技能
- **不用手动加载**：启动时所有技能的元数据（名称+描述）已注入 agent 的 system prompt，agent 会根据你的任务**自动匹配并加载**合适的技能（LLM 自路由）——直接正常提问即可，比如问"总结一下钙钛矿太阳能电池稳定性的热降解机理"，agent 会自动加载 `perovskite-domain` + `citation-format` 并按规范回答。
- 加载状态跨会话保留（`/new` 不清空，重启 CLI 会清空重新加载）。

**缓存不破**：元数据表只进 system prompt 前缀（字节稳定，服务端缓存可命中）；skill 正文只走工具返回/每轮 user 消息（区2），动态内容永远不进前缀。

### MCP 客户端（v0.0.14）

**什么是 MCP**：Model Context Protocol——外部 server（如本地数据库、专用 API 封装）通过标准协议把自己的能力暴露成工具，PhySc 启动时自动连接并把这些工具注册进工具集，agent 像用内置工具一样自动调用。

**配置**：编辑项目根 `phxsc.mcp.json`，支持两类 server：

```json
{
  "servers": {
    "fixture": {
      "type": "stdio",
      "command": [".venv/bin/python", "tests/fixtures/mcp_fixture_server.py"],
      "allowed_modes": ["plan", "investigate"]
    },
    "remote": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {"Authorization": "Bearer xxx"}
    }
  }
}
```

- `stdio`：本地子进程（`command` 必填，可带 `env`）
- `http`：远程 HTTP 端点（`url` 必填，可带 `headers`）——只做请求-响应（POST JSON），SSE 流式暂不支持
- `allowed_modes`：该 server 的工具在哪些模式可用（默认 `["plan","investigate"]`，**typeset 模式默认不可用**；不配置即用默认）
- 配置文件缺失/写坏不影响启动（按无 MCP 处理）；单个 server 连接失败也不阻塞其他

**使用**：
- `/mcp list` 或 `/mcp status` —— 查看已连接 server、每个 server 的工具数、连接失败原因
- 工具以 `mcp_<server>_<工具名>` 命名注册，描述带 `[MCP <server>]` 前缀，agent 自动识别调用

## 3. 三模式能力矩阵（当前真实状态）

| | plan（只读侦察） | investigate（全功能） | typeset（文档生成） |
|---|---|---|---|
| 搜 arXiv 文献 | ✅ | ✅ | ✅ |
| 检索/写入长期记忆 | ✅ | ✅ | ✅ |
| Zotero 只读查询 | ✅ | ✅ | ✅ |
| 读笔记 / 列笔记 | ✅ | ✅ | ✅ |
| 下载论文 PDF | — | ✅ | — |
| 解析 PDF 入证据库 | — | ✅ | — |
| 写笔记 | — | ✅ | — |
| 写规划 plans/ | ✅（plan_write） | — | — |
| 读规划 plans/ | — | — | ✅（plans_read） |
| Markdown → PPTX | — | — | ✅（typeset_generate） |

> ⚠️ plan 模式写权限仅限 plans/ 目录与记忆库（evidence/记忆）；typeset 模式写权限仅限 typeset/；investigate 沙箱内全权。

## 4. 能做什么（工具视角）

### 学术检索
- **搜文献**：直接问"帮我搜 2024-2026 钙钛矿太阳能电池稳定性方向的论文"——agent 调 `arxiv_search` 返回论文列表（标题/作者/摘要/链接）
- **下载论文**："下载 2509.13700 这篇论文"——`paper_download` 从 arXiv 下载 PDF 到 `workspace/papers/`（已下载过会自动跳过）
- **定时抓新论文**：`/schedule add "每天 9:00" 钙钛矿稳定性`——每天 9 点按主题检索 arXiv，摘要追加到 `workspace/notes/daily/YYYY-MM-DD.md`

### 论文阅读
- **解析入库**："解析 papers/2509.13700.pdf"——`pdf_parse` 提取段落，按页码切分存入**证据库**（evidence：片段+页码，溯源闸门的弹药）
- **智能下载**：直接说"解析 2601.12345 这篇"（文件还没下载）——`pdf_parse` 检测到文件缺失且 arXiv ID 合法时，**自动先下载再解析**
- **定向阅读总结**："总结 papers/2509.13700.pdf，存入笔记"——agent 会解析 → 按**三段式**（贡献 / 与你的关系 / 可改进点）总结 → `notes_write` 落盘 `workspace/notes/`

### 记忆与笔记
- **记住（重要偏好）**："记住：我的研究方向是钙钛矿太阳能电池稳定性"——`remember` 写入长期记忆
- **记住（注入轨）**：说"记住：这是重要事项……"（agent 会调 `remember` type=important）——进 system prompt，**每轮对话都可见**（限额 800 字符）
- **检索记忆**："我之前看过什么相关论文？"——`memory_search` 语义检索（同 query 有持久缓存，秒回）；记忆多时自动启用**混合检索**（关键词 FTS + 语义双路，召回归一相关度排序）
- **笔记**："把刚才的总结存成笔记 xxx" / "读一下笔记 xxx" / "列出所有笔记"

### 长任务（plan-then-execute，自动触发）
输入含 **规划/分步/先计划/综述** 等词、或有 3+ 个子目标、或超过 200 字符的复杂任务，agent 自动两阶段：
1. **阶段1（规划）**：只用只读工具制定计划 → 落盘 `workspace/plans/`
2. **阶段2（执行）**：重建上下文，按计划全工具执行；每 3 步把中间结果摘要写回计划文件（防中途丢失）
3. 回答末尾提示"执行进度已记录：plans/<文件名>"

> 可用 `PHXSC_LONGTASK=0` 环境变量禁用自动触发。

### 方案拷问（grill-me 精简版，v0.0.2 起）
plan / investigate 模式下，agent 对复杂需求**动手前先输出关键点**：
- 关键假设——需求隐含了什么假设？不成立会怎样？
- 范围风险——会不会做多/做少？需要澄清的直接问

简单任务（单步、无歧义）自动跳过拷问，直接执行。

### 文档生成（typeset）
```bash
/typeset
> 把笔记 钙钛矿热降解研究现状综述 做成 PPT
```
`typeset_generate` 读取 `notes/` 或 `plans/` 下的 Markdown，生成 PPTX 到 `workspace/typeset/`。支持三套配色主题：
- `academic`（学术蓝，默认）
- `deep`（深色科技）
- `warm`（暖色简约）

默认 `style=auto` 按内容关键词自动选（综述→academic），也可让 agent 显式指定"用深色主题做"。

### 严谨模式（溯源闸门）
```bash
/gate 请严谨回答：这篇综述里的热降解机理有哪些？
```
`/gate <问题>` 前缀触发**本轮**引用校验：agent 会先检索收集证据再作答，**最终回答**过一遍引用校验：把回答 + 证据库片段发给 LLM 判断，无证据支撑的论断会列出警告（"⚠️ [溯源闸门] 以下论断未通过引用验证"）。
- 适合：综述、答辩、写材料等需要严谨输出的场景
- 注意：多一次 LLM 调用，耗 token；**默认不校验**，需要严谨输出的那轮在问题前加 `/gate` 即可

## 5. 示例工作流

```bash
export DEEPSEEK_API_KEY=sk-xxx
.venv/bin/python -m phxsc.cli --mode plan

# 1. 侦察（plan 模式）
> 帮我搜 3 篇 2024-2026 钙钛矿太阳能电池稳定性方向的综述
> 记住：我在调研钙钛矿太阳能电池稳定性

# 2. 干活（切 investigate）
> /investigate
> 下载 2509.13700
> 总结 papers/2509.13700.pdf 并存入笔记

# 3. 长任务（自动两阶段）
> 综述钙钛矿热降解研究现状，整理成笔记

# 4. 严谨输出（请求级闸门，本轮校验）
> /gate 这篇综述里提到的热降解机理有哪些？

# 5. 生成 PPT
> /typeset
> 把笔记 钙钛矿热降解研究现状综述 做成 PPT
```

## 6. 数据存放位置

```
workspace/              # 默认工作目录（--workdir 或 PHXSC_WORKDIR 可改）
├── papers/      # 下载的论文 PDF（arxiv_id.pdf）
├── notes/       # 笔记（.md）
│   └── daily/   # 定时任务速报（YYYY-MM-DD.md）
├── plans/       # 长任务规划 + plan_write 产出
├── typeset/     # PPTX 生成产出
└── tmp/         # 临时文件 + telemetry.jsonl（token/成本/命中率）
workspace/memory.db      # 长期记忆 + 证据库 + 论文记录（SQLite）
workspace/scheduler.db   # 定时任务存储
workspace/embed_cache.db # query 向量缓存
```

> 所有文件操作限定在 workspace 沙箱内；`~/.ssh`、`~/.hermes`、`~/.config` 等敏感路径永远拒绝写入（沙箱黑名单）。

## 7. 常见问题

| 问题 | 解决 |
|------|------|
| `缺少环境变量 DEEPSEEK_API_KEY` | 先 `export DEEPSEEK_API_KEY=sk-xxx` 或项目根建 `.env` 再启动 |
| 报错 `Authentication Fails` | key 不对或过期，检查 key 值 |
| Zotero 相关提示"不可访问" | 正常——本机没有 Zotero 数据库。接入后设置 `ZOTERO_PROFILE` 环境变量指向 Zotero profile 目录 |
| 记忆检索首次略慢（~10s） | 智谱 API 首次 TLS 连接建立，之后每次往返 <0.1s（进程内缓存秒回） |
| 想完全离线（不用智谱 API） | `export PHXSC_EMBED_BACKEND=local`——切本地 bge 模型（首次加载 ~50s，之后进程内缓存） |
| 回答到一半提示"达到最大步骤限制" | 任务步骤超过 15 步，拆小一点问 |
| 工具连续失败 2 次 | agent 自动中断（防死循环），屏幕上会显示红色 `⚠ 工具 xxx 失败：原因` + 黄色 `提示：下一步怎么做` |
| arXiv 搜索超时/失败 | 国内网络 https export.arxiv.org 被阻断，走 Clash 代理后重试 |
| `/schedule add` 报非法 cron | 用引号包住带空格的 cron：`/schedule add "每天 9:00" 主题`（不带引号需自行把空格合并，如 `0 9 * * *`） |

## 8. 环境变量

| 变量 | 作用 |
|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key（必填） |
| `ZHIPU_API_KEY` | 智谱 embedding key（不设则语义缓存降级为 exact-only） |
| `PHXSC_WORKDIR` | 工作目录（默认 `<项目根>/workspace`；`--workdir` 显式传参优先） |
| `PHXSC_DB` | 记忆库路径（默认 workspace/memory.db） |
| `PHXSC_EMBED_BACKEND` | `zhipu`（默认，智谱 API）/ `local`（本地 bge 离线） |
| `PHXSC_LONGTASK` | `0` 禁用长任务两阶段自动触发 |
| `PHXSC_HYBRID_THRESHOLD` | 记忆混合检索启用阈值（记忆数超过才走 FTS+向量双路，默认 1000；设 0 强制混合） |
| `PHXSC_TELEMETRY_PATH` | telemetry 输出路径（默认 workspace/tmp/telemetry.jsonl） |
| `PHXSC_ENV_FILE` | .env 文件路径（默认 <项目根>/.env） |
| `ZOTERO_PROFILE` | Zotero profile 目录（含 zotero.sqlite） |

## 9. 已知限制（尚未实现，后续补齐）

- ✅ PPT 审美升级（Day 11 已完成：单 accent 锁定）
- ✅ 研究脉络追踪（Day 8 已完成：OpenAlex 引用图 + HTML 可视化）
- ✅ 会话管理（Day 9 已完成：/sessions /search /resume /fork /model /stop；v0.0.21 加自动命名+标题列）
- ✅ 论文图表分析 figure_analyze（Day 10 已完成：tesseract OCR + Gemini 视觉兜底）
- ✅ skill 全文按需加载（Day 10.5 已完成：大 skill 不再被 2KB 截断）
- ✅ PDF 报告导出（Day 11 已完成：typeset pdf 子命令）
- ✅ 请求级闸门 /gate <问题>（前缀化触发，替代 /gate on|off）
- ✅ UI 重构（Textual Tab + 大布局，含多轮用户实测修复）
- ❌ 权限审批机制（Day n：高风险操作 CLI 确认，非 tty 自动拒绝）
- ❌ Zotero 完整导入（只有只读查询；接口已预留，Day x）
- ❌ Telegram/微信接入（Day x）

---

遇到问题可以看 `docs/PROJECT_STATUS.md`（项目状态）和 `PLAN.md`（技术设计）。

---

## 10. TUI 终端工作台（v0.0.21）

PhySc 现在提供 **CLI/TUI 双轨**：默认在交互终端（tty）自动进入 Textual TUI；管道/脚本场景自动回落 Rich CLI。核心引擎（agent loop / cache / memory / tools / registry）零改动，UI 只是观察层与控制层。

### 10.1 启动与回落

```bash
.venv/bin/python -m phxsc.cli               # tty 下自动进 TUI
.venv/bin/python -m phxsc.cli --no-tui      # 强制走 Rich CLI（管道/脚本/无终端）
printf '/plan 帮我搜论文\n/exit\n' | .venv/bin/python -m phxsc.cli --no-tui  # 非交互管道
```

非 tty（stdin 非终端）自动回落 Rich CLI，等价于 `--no-tui`。

### 10.2 布局（响应式断点）

| 宽度 | 布局 |
|------|------|
| <80 | 只对话 + 输入框 + 极简状态栏（仅 mode+model），隐藏 Tab 切换条 |
| 80-99 | 完整状态栏，隐藏右侧 Inspector |
| 100-119 | Inspector 窄版（28 列） |
| 120-139 | 完整四区（默认）：Header + Chat/Activity/Status + Inspector + Composer + 状态栏 |
| ≥140 | Inspector 加宽 |

### 10.3 模式切换

三种模式对应三种低饱和 accent 色（蓝/绿/紫）：

- **PLAN**（蓝）只读侦察 · 侧边 Research Plan 面板
- **INVESTIGATE**（绿）全功能 · Evidence Stream 面板
- **TYPESET**（紫）文档生成 · Document Preview 面板

切换方式：`Tab` / `Shift+Tab` 循环，或输入 `/plan` `/investigate` `/typeset`。切换保留会话上下文与历史。

### 10.4 命令面板 / 会话 / 帮助

- `Ctrl+P` 命令面板（模糊搜索，回车填入 Composer 而非立即执行）
- `Ctrl+L` 会话列表（`/sessions`）
- `Ctrl+M` 模型选择
- `?` 帮助（从 `/help` 命令同步生成）

### 10.5 快捷键表

| 键 | 功能 |
|----|------|
| `Tab` / `Shift+Tab` | 循环切换模式 |
| `Ctrl+P` | 命令面板 |
| `Ctrl+L` | 会话列表 |
| `Ctrl+M` | 模型选择 |
| `Ctrl+C` | 中断当前任务（显示 STOPPING… → STOPPED） |
| `Ctrl+J` | 回对话底部 |
| `↑` `↓` | 输入历史 / 列表导航 |
| `Enter` | 发送 |
| `Esc` | 关闭浮层 |
| `Ctrl+Shift+V` | 粘贴（Windows Terminal/WSL 终端层 bracketed paste） |
| `Ctrl+Shift+C` | 复制选中文本（终端层） |
| `?` | 帮助 |

> 说明：TUI 输入框的 `Ctrl+V` 无效属预期行为——Textual 的 App 内剪贴板不含 OS 剪贴板。请一律用终端层 `Ctrl+Shift+V` 粘贴、`Ctrl+Shift+C` 复制。

### 10.6 过程可视化

- 对话流混排工具卡片（折叠友好语义，展开真实 tool 名 + 耗时）、Thinking 折叠块、Paper 卡片、Citation Gate 5 步流程
- `ACTIVITY` 标签页：研究过程时间线（真实工具名 + 耗时，最新优先）
- `STATUS` 标签页：session / mode / model / thinking / voice / gate / context / cache / workspace / skills / MCP / scheduler 全量状态
- 语义缓存命中在答案前显示 `⚡ semantic cache · 0.96`；状态栏 cache% 常驻

### 10.7 真实交互验收

TUI 渲染逻辑由 Pilot 测试全覆盖；**真实终端交互（键盘/滚动/色彩观感）请人工验收**，10 场景操作见 `docs/UI_DESIGN.md` §9。

---

遇到问题可以看 `docs/PROJECT_STATUS.md`（项目状态）和 `PLAN.md`（技术设计）。
