# PhySc-agent 架构

> 本文档描述 v0.1.0 的核心架构设计。面向想要理解或扩展本项目的读者。

## 设计原则

1. **前缀缓存神圣**：动态内容绝不进 system prompt——前缀字节稳定是 DeepSeek 服务端缓存命中的根基
2. **Narrow waist**：一切能力收敛到"工具"这一窄接口（工具注册器 + 模式过滤），agent loop 不感知具体工具
3. **能力在边缘**：核心 loop 保持最小，PDF 解析/笔记/排版等能力全部以工具形态挂在边缘

## 四区上下文模型

（借鉴 Reasonix Cache-First 设计，MIT）

```
区1 IMMUTABLE PREFIX：system prompt + tools schema（字节稳定 = DeepSeek 服务端缓存命中 $0.0028/M）
区2 APPEND-ONLY LOG：对话历史（严格 role 交替，只追加不重写 → 保留前序前缀）
区3 VOLATILE SCRATCH：thinking 等（永不进上下文）
+ 动态内容（记忆/检索结果）一律走工具返回，不碰前缀
```

**铁律**：动态内容绝不进 system prompt。记忆注入轨（重要偏好 ≤800 字符）只在启动/切模式时组装一次。

## 缓存分层（exact → semantic → LLM）

```
层1 DeepSeek 服务端 prefix 缓存（零代码）：前缀字节稳定 → 命中 98% 折扣
层2 ExactCache（SQLite）：同 query（hash 含模式）→ 直接返回历史最终回答
层3 SemanticCache（SQLite + numpy）：query embedding 桶内余弦 top1，score ≥ 0.93 且实体差异守卫放行才命中
    └─ 实体差异守卫：difflib 差异片段正则检测化学式/年份/数值/型号/ID → 差异含实体强制 miss
命中时零 LLM 调用；上下文依赖 query（指代/太短）自动跳过；/cache stats|clear 查看与确认式清空
```

gate 开启时 semantic 缓存全旁路（不查不写），保证严谨输出永远走 LLM + 引用校验。

## 模块总览

```
src/phxsc/
├── agent/
│   ├── loop.py          # ReAct 主循环：max_steps=15、storm 防重复、scavenge 扫回、长任务两阶段
│   ├── tools.py         # @tool 注册器：schema 自动生成 + 模式过滤
│   ├── context.py       # 四区上下文：role 交替校验、窗口裁剪、LLM 摘要压缩
│   ├── modes.py         # plan / investigate / typeset 三模式
│   ├── moa.py           # MoA 主控-下手多 agent 编排（/moa）
│   ├── thinking.py      # thinking 档位注入（budget_tokens / reasoning_effort 双形态）
│   └── longtask.py      # 长任务两阶段（简单任务豁免判定）
├── memory/
│   ├── store.py         # SQLite：memories/evidence/papers + FTS5 触发器
│   ├── embed.py         # 智谱 embedding-3 API / 本地 bge 开关
│   ├── retrieve.py      # 余弦 top-k 检索（+ query 向量持久缓存）
│   ├── hybrid.py        # 混合检索：FTS5 BM25 ∪ 向量双路 → RRF 融合
│   └── inject.py        # 注入轨：important 记忆进 system prompt（≤800 字符）
├── tools/
│   ├── arxiv.py         # arXiv 检索（stdlib urllib + Atom 解析）
│   ├── paper.py         # PDF 下载 + 缓存（已存在跳过）
│   ├── pdf.py           # pymupdf 解析 → evidence 入库（片段+页码）
│   ├── oa.py            # OpenAlex OA 直链查询 + 下载
│   ├── scihub.py        # Sci-Hub 三级保底链（直连 → altcha 直解 → Chrome 兜底）
│   ├── web.py           # Tavily 搜索 + DuckDuckGo 兜底
│   ├── notes.py         # 笔记读写（沙箱内）
│   ├── typeset.py       # Markdown → PPTX（微软雅黑/Calibri，16:9）
│   ├── vision.py        # figure_analyze：tesseract OCR + GLM-4V 兜底
│   ├── dedup.py         # SimHash 查重引擎
│   ├── lineage.py       # OpenAlex 引用网络追踪
│   ├── plan.py          # 长任务计划解析
│   ├── memory.py        # 记忆读写工具
│   └── zotero.py        # Zotero 只读查询
├── gates/
│   └── citation.py      # 引用溯源闸门（请求级 /gate <问题> 触发）
├── scheduler/
│   └── jobs.py          # APScheduler：/schedule add "每天 9:00" <topic>
├── cache/
│   ├── exact.py         # exact cache：同 query 直接返回
│   ├── embed_cache.py   # query 向量持久缓存（同 query 零推理）
│   ├── semantic.py      # 语义缓存：桶内余弦 top1 ≥0.93 + LRU 500
│   └── entity_guard.py  # 实体差异守卫：差异含实体强制 miss
├── mcp/
│   ├── client.py        # MCP 客户端（stdio + HTTP 双 transport）
│   ├── config.py        # phxsc.mcp.json 配置解析（缺失/损坏回落空配置）
│   ├── registry.py      # 工具动态注册（mcp_<server>_<工具名>）
│   └── transport.py     # stdio / Streamable HTTP 简化子集
├── ui/                  # Textual TUI
│   ├── app.py           # PhyScApp 主应用
│   ├── events.py        # 25 事件 EventBus（线程安全）
│   ├── state.py         # UIState（事件映射 + status_line）
│   ├── theme.py         # 11 色板 token（三模式三低饱和色）
│   ├── keymap.py        # 键位绑定
│   ├── widgets/         # header/composer/status_bar/inspector/tool_card/thinking_block 等
│   ├── screens/         # chat/activity/status 三页
│   └── overlays/        # command_palette/session_picker/help/model_picker
├── providers.py         # 多 provider 注册表（key 只存环境变量名，永不明文）
├── sessions.py          # 会话存储（sessions.db + FTS5）
├── settings.py          # 用户级设置持久化（~/.phxsc/settings.json）
├── skills/              # skill 体系：scan.py（扫描/解析/元数据表）+ loader.py（正文加载）
├── splash.py            # 启动动画（三档降级 + alternate screen）
├── telemetry.py         # token/成本/缓存命中率 → workspace/tmp/telemetry.jsonl
├── cli.py               # CLI 入口：命令分发 + Rich 非 TUI 模式 + --no-tui
└── sandbox/
    └── paths.py         # 沙箱：白名单（realpath 前缀）+ 黑名单（home 敏感路径）双机制
```

## 关键机制

### ReAct loop

- max_steps=15；storm 窗口防重复动作；scavenge 失败后扫回
- 长任务两阶段：阶段1 只读侦察出计划 → 落 plans/ → 阶段2 重建上下文全工具执行，每 3 步写回进度摘要
- LLM 请求显式超时（流式 60s / 非流式 300s），中断检查点三处（流式回退前/非流式返回后/每工具后）——SSE stall 也不会卡死
- 缓存命中（exact/semantic）零 LLM 调用；gate 轮全旁路

### TUI 事件层

- 25 个事件常量 + payload 契约 + EventBus（线程安全、订阅者异常隔离）
- 生产路径发布（loop/cache/gate/evidence/artifact），UIState 全量映射
- CLI 与 TUI 双轨：`--no-tui` 保留 Rich 模式；核心引擎零改动（ui/ 只 import cli 层接口）

### 沙箱

- 白名单：workdir realpath 前缀内可写
- 黑名单：home 敏感路径拒绝
- 所有文件工具（notes/plan/typeset/download）统一走 safe_read_path / safe_write_path

### MoA（/moa）

- 星型协调：主控拆解 → N 模型并行（默认 3，上限 4）→ 聚合
- SharedSeenSet 线程安全防重复登记（source_id 多模式：arXiv/DOI/URL）
- worker mini-loop ≤3 轮带工具；失败降级 failed 协议；生成场景落盘 notes/

### skill 体系

- SKILL.md frontmatter 取 name/description/version（对齐 agentskills.io 开放标准）
- 元数据表（~150B/个）启动一次性进 system prompt（区1，不破缓存）
- 正文走 skill_load 工具（区2）；/skill load 加入注入轨（全文注入，≤8 个）
- 双目录：项目级 skills/ + 用户级 ~/.phxsc/skills/（PHXSC_SKILLS 覆盖）

## 数据存储

| 文件 | 内容 |
|------|------|
| `workspace/memory.db` | 记忆/evidence/论文元数据（SQLite + FTS5） |
| `workspace/sessions.db` | 会话历史（跨会话检索） |
| `workspace/exact_cache.db` / `semantic_cache.db` / `embed_cache.db` | 三层缓存 |
| `workspace/scheduler.db` | 定时任务 |
| `workspace/notes/` `plans/` `papers/` `typeset/` | 产出物（沙箱内） |
| `~/.phxsc/settings.json` | 用户级设置（thinking 档位/provider/model） |
| `~/.phxsc/providers.json` | 自定义 provider |
| `~/.phxsc/skills/` | 用户级私有 skill |
