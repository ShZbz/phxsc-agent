# PhySc-agent UI 重构设计方案（UI_DESIGN）

> 2026-08-12 · Day 13 UI 重构设计稿 · 状态：已与用户确认，待实施
> 综合 6 份外部 AI 方案（见 §15 参考索引），用户拍板要点：三模式三低饱和色 / 分批推进 / 测试方案 A（逻辑单测+Pilot，不引入 snapshot）/ CLI+TUI 双轨
> 配套文件：`docs/ui-refs/`（六份原始方案存档，文件名含重要级/内容/借鉴点）

---

## 1. 定位与原则

**目标**：把 PhySc 从"终端聊天框"升级为"科研终端工作台"（Research Agent TUI，对标 OpenCode/Reasonix 的交互品质），CLI/TUI 双轨并行，核心引擎零改动。

核心隐喻（与 OpenCode 的 code→diff 相反）：
```
researcher → question → literature → paper → evidence → reasoning → note → plan → typeset artifact
```

铁律（继承 chatgpt1-1 §2 + 项目宪法）：
1. 不重写 Agent loop；不改 cache/memory/tools/registry/API 契约
2. 不改 mode 权限模型（plan=只读侦察、investigate=全功能、typeset=文档生成）
3. 不删现有 slash command；非 tty 自动 fallback Rich CLI；新增 `--no-tui` 参数
4. 不新增 emoji 装饰、不做网页风/VS Code clone/"满屏边框 Panel"廉价 TUI
5. 动态 memory/检索内容永不进 system prompt（前缀缓存不破）
6. UI 是 Agent 的观察层和控制层，不是新的 Agent runtime

## 2. 架构：事件层 + 双 renderer

```
AgentLoop（不动）→ EventBus 事件层（cli 层新增）→ 双 renderer
                                     ├─ TextualRenderer（新 TUI，isatty 时启用）
                                     └─ RichRenderer（现有打印逻辑重构为事件消费，--no-tui）
```

- 新增 `src/phxsc/ui/` 包：`app.py / state.py / events.py / theme.py / keymap.py` + `screens/` + `widgets/` + `overlays/`
- 现有 `_PrintingRegistry` 改造为事件发布点（adapter 包裹，不破坏其行为与既有测试）
- `/schedule /skill /mcp /cache` 等命令 handler 原样复用，TUI Composer 只做输入转发
- 依赖仅加 `textual`（锁定版本；任务卡注明 on_mount 等 API 差异坑）
- 严禁在 Textual 里 grep stdout——必须走结构化事件

### 事件契约（events.py）

| 事件 | 载荷 |
|---|---|
| AgentStarted | — |
| AgentCompleted | duration, artifacts[] |
| AgentInterrupted | reason |
| ThinkingStarted / ThinkingEnded | level |
| ToolStarted | name, args 摘要 |
| ToolSucceeded | name, duration, result 摘要 |
| ToolFailed | name, error, reason, fix_hint（结构化三件套） |
| EvidenceFound / PaperFound | count / paper 元数据 |
| ArtifactCreated | path, kind |
| CacheHit / CacheMiss | kind(exact/semantic/prefix), score |
| ModeChanged / SessionChanged / ModelChanged / VoiceChanged / ThinkingChanged | 新值 |
| GateStarted | question |
| TaskPhaseChanged | phase, step, total, label |
| ContextUsageUpdated | used/total/percent |
| ApprovalRequired / WaitingUser | action, risk |
| Error | message |

### UIState（state.py）

显示状态，非 LLM context：mode / session_id / session_title / model / thinking_level / voice / gate / running / phase / current_tool / tool_history / task_progress / context_usage / cache_stats / cost / elapsed / selected_object / active_tab / overlay

## 3. 布局规格

### 3.1 总览（≥120 列完整布局）

```
┌ HEADER：PHYSC · 会话名 │ PLAN INVESTIGATE TYPESET │ 模型·thinking·voice ┐
├────────────────────────────────┬───────────────────────────┤
│ CHAT / ACTIVITY / STATUS（Tab）│  INSPECTOR（contextual）   │
│ ┌ 对话区 ────────────────┐     │   无选中：研究上下文        │
│ │ 消息/工具卡片/进度     │     │   选中 paper/evidence/      │
│ └────────────────────────┘     │   artifact：动态切换对象信息 │
│                                │                           │
├────────────────────────────────┴───────────────────────────┤
│ COMPOSER（多行输入 + 模式 badge + 动态快捷键提示）           │
├─────────────────────────────────────────────────────────────┤
│ STATUS：模式│运行态│模型│ctx%│cache%│成本│耗时                │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 响应式断点

| 宽度 | 布局 |
|---|---|
| <80 | 只 Chat + Composer + 极简状态栏（mode+model） |
| 80-99 | + 完整状态栏；隐藏 Inspector |
| 100-119 | Inspector 窄版（28 列） |
| 120-139 | 完整四区（默认） |
| ≥140 | Inspector 加宽 |

禁止：文字硬截断 / panel 重叠 / 横向滚动为主交互 / 输入框窄到不可用。

### 3.3 Tab 三页

- **CHAT**：对话 + 工具卡片 + 任务进度（主视图）
- **ACTIVITY**：研究过程时间线（真实工具名 + 耗时，可搜索，最新优先）——"研究过程日志"不是 debug log
- **STATUS**：session / mode / model / thinking / voice / gate / context / cache / workspace / skills / MCP / scheduler 全量状态页

## 4. 视觉系统

### 4.1 色板（theme.py token）

| token | 值 | 用途 |
|---|---|---|
| bg | `#0d0f11` | 基底（graphite，非纯黑） |
| text1 | `#e5e7eb` | 一级文本 |
| text2 | `#9ca3af` | 二级文本 |
| text3 | `#6b7280` | 三级/禁用 |
| border | `#26292d` | 分割线 |
| **mode-plan** | `#7aa2f7` | 低饱和蓝（plan accent） |
| **mode-investigate** | `#7fd1ae` | 低饱和绿（investigate accent） |
| **mode-typeset** | `#a48fe0` | 低饱和紫（typeset accent） |
| success | `#4ade80` | ✓ / 成功 |
| warning | `#e5c07b` | 警告 / gate / cache hit |
| error | `#e06c75` | 失败 |

规则：slate 基底 + 三模式三低饱和 accent（已拍板）；每屏只有一个模式 accent 在起作用；语义色克制使用；颜色不作唯一信息载体（配符号）。

### 4.2 符号与排版

- 符号白名单：`✓ [x] [ ] ! × → ⚡`；禁 🧠🧪🤖 类 emoji
- 分隔用 `───`，不用 `╔╗╚╝` ASCII 框
- 层级：TITLE bold / SECTION bold+accent / PRIMARY normal / SECONDARY dim / META dim
- 双层信息：默认友好语义（"Reading paper"），展开才显示技术细节（`tool: pdf_parse · pages 1-38 · 1220 evidence · 4.2s`）

## 5. 模式系统

| 模式 | accent | 语义 | UI 差异 |
|---|---|---|---|
| PLAN | 蓝 | READ ONLY · 侦察/规划 | 侧边 Research Plan 面板（步骤 ✓/[x]/[ ]）；工具默认折叠 |
| INVESTIGATE | 绿 | FULL ACCESS · 下载/解析/记忆/笔记 | Evidence Stream 面板；工具默认展开 |
| TYPESET | 紫 | DOCUMENT OUTPUT · PPTX/PDF | Document Preview 面板（slides 树）；Markdown 预览 |

- 切换：Tab 循环三模式 + 保留 `/plan /investigate /typeset` 命令；轻量 toast（`Mode switched PLAN → INVESTIGATE`），非页面刷新
- 同一 session 保持 conversation/history/context/artifacts，只改变 tool availability 与 mode indicator
- `[GATE]` 启用：黄色 badge + 状态栏显示 `cache bypass`（gate 旁路 semantic cache 必须让用户感知）

## 6. 核心组件规格

### 6.1 Conversation
- terminal-native 无气泡：用户消息紧凑（`You ─── 内容`）；agent 消息带结构（结论/证据/Artifacts 区）
- agent 输出不套"当然可以/首先/最后"模板——UI 本身提供结构

### 6.2 Tool 卡片（默认折叠一行）
```
✓ arxiv_search · 12 results · 0.84s
```
展开：query / 结果摘要 / 耗时；失败显示 `! TOOL FAILED` 结构化错误框（error/reason/fix_hint）+ `[Enter] details`，绝不裸 Traceback

### 6.3 Thinking
- 默认一行 `reasoning · high`（折叠）；展开灰色斜体
- 与最终回答严格分离，不混入主内容

### 6.4 Cache
- 命中：答案前 `⚡ semantic cache · 0.96`；exact：`↻ exact cache`
- 详情（overlay）：current query / matched query / score / entity guard / LLM skipped
- 状态栏常驻 `cache 93%`（prefix 口径）

### 6.5 Citation Gate 流程（/gate <问题>）
5 步可视化：collect evidence → extract claims → match → verify → rewrite
```
CITATION GATE
✓ 18 evidence blocks
✓ 7 claims extracted
✓ 6 supported / ! 1 verify
最终：VERIFIED · 7/7 claims · 4 sources
```
- 普通模式引用渲染：`[arXiv:xxxx, p.4]` 内联即可，不强制学术格式
- 原文证据放 Inspector，不撑爆正文

### 6.6 Paper / Evidence
- 搜索结果轻量卡片：`[01] 标题 · 期刊 · 年份 · relevance` + `[r]read [e]evidence [l]lineage`
- Evidence 折叠：`▸ [1] Nature Physics · p.4` → Enter 展开原文 + actions
- Paper 详情（Inspector）：authors/journal/year/pages/evidence 数/状态（Downloaded✓ Parsed✓ Read✓）

### 6.7 长任务（两阶段）
```
TASK · 阶段1 PLAN ✓ 全部完成
阶段2 INVESTIGATE
  ✓ Download papers
  [x] Parse evidence
  [ ] Write synthesis
Progress 2/3 · Reading paper 4/7
```
有"项目进度感"，不是几十条普通 tool call

### 6.8 Artifact / Lineage / Figure
- Artifact 视图：PLAN/PAPERS/NOTES/TYPESET/LINEAGE 分类列出产物路径（可选中进 Inspector）
- Lineage：终端内树状摘要（upstream/downstream/key nodes 计数）+ HTML 交互图由外部浏览器查看
- Figure：明确区分 OCR / Vision / LLM interpretation / Source evidence 四来源，避免混淆

### 6.9 Command Palette（Ctrl+P）
模糊搜索，分组：Modes / Session / Research / System；支持 `/` 弹出斜杠命令补全（含参数提示如 `/gate <问题>`）

### 6.10 Overlay 规范
统一 modal：command palette / session picker / help / model picker / cache 详情 / evidence / paper；≤70% 终端宽，背景 dim，Esc 关闭

### 6.11 四态设计（每个组件至少四态）
empty / loading / success / error（如 Evidence：`No evidence` → `Extracting…` → `37 blocks` → `PDF parsing failed`）

## 7. 状态机（UI 推导，不强迫 backend 引入）

IDLE → UNDERSTANDING → PLANNING → SEARCHING → READING → ANALYZING → VERIFYING(GATE) → WRITING → TYPESETTING → DONE
横向分支：WAITING_USER / ERROR / INTERRUPTED
由现有 tool/task 状态推导，不改 core loop。

## 8. 快捷键

| 键 | 功能 |
|---|---|
| Tab / Shift+Tab | 切换模式（循环） |
| Ctrl+P | command palette |
| Ctrl+L | sessions 列表 |
| Ctrl+C | 中断（=/stop，显示 STOPPING… → STOPPED 无异常） |
| Ctrl+J / End | 回对话底部 |
| ↑ ↓ | 输入历史 / 列表导航 |
| Enter / Shift+Enter | 发送 / 换行 |
| Esc | 关闭 overlay |
| ? | help（首次/按需，不常驻占行） |

实现前核对 Textual 默认 key binding 冲突；contextual 快捷键：paper 选中时 `r/e/n/l/d`，evidence `o/c/r`，artifact `o/c`。

## 9. 测试与验收（三层，用户已拍板方案 A）

1. **逻辑单测**：EventBus 事件序列、UIState 更新、keymap、theme token 一致性（新增 ~30-50 个）
2. **Textual Pilot 功能测试**（内置 run_test，不引入 snapshot 依赖）：启动/模式切换/工具状态流转/尺寸断言（80x24 下 Inspector 隐藏等属性断言）
3. **真实冒烟**：plan 搜索 → investigate 下载解析 → /gate 校验 → typeset 生成各跑一遍 + 尺寸矩阵截图留档

### 尺寸矩阵
80x24 / 100x30 / 120x30 / 140x40 / 160x50：不得溢出、不破版、关键信息可见。

### 验收场景（chatgpt1-1 §91-101 精简）
1. plan 模式搜索+规划 → Research Plan + plans/xxx.md，全程只读
2. investigate 下载+解析 → Notes + evidence 计数
3. typeset notes→PPT → ✓pptx ✓pdf
4. /gate 问题 → GATE·STRICT → VERIFIED 7/7
5. Ctrl+C 中断 → STOPPED，无异常
6. 语义缓存命中 → ⚡ 0.96 + Activity 里 cache 详情
7. figure_analyze → OCR/Vision/Evidence/Interpretation 四来源分离
8. /sessions → 列表 + Enter 恢复
9. 80x24 窄屏不破版
10. 140x40 宽屏 contextual inspector 动态切换（选中 evidence→EVIDENCE，选中 paper→PAPER）

## 10. 风险与边界

| 风险 | 对策 |
|---|---|
| textual 版本 API 差异 | 任务卡锁定版本；注明 on_mount 等旧写法坑 |
| `_PrintingRegistry` 被既有测试覆盖 | 事件层 adapter 包裹而非替换，旧行为不变 |
| cli.py 1270 行命令逻辑复用 | TUI Composer 只做输入转发，handler 原样调 |
| TUI 侵入 core | ui/ 包只 import cli 层已有接口；违反即打回 |
| 装饰性过度（emoji/边框/渐变） | 反 AI slop 清单进任务卡验收项 |
| 事件发布点断链（早期只接 tool 三事件，cache/evidence/artifact/gate 等 15+ 事件生产路径无发布点） | 补全发布点（cli/loop 外层 wrapper，不动 core）；独立审计核验确认 |

明确不做（anti-scope）：不做网页版 / 不引入 Nerd Fonts 依赖 / 不做像素级 snapshot 测试 / 不改 Zotero 完整导入 / 不做多 agent。

## 11. 设计参考

UI 设计借鉴了以下公开来源的思路（仅设计思路，无代码拷贝）：

- **ChatGPT / Claude 类对话产品**：研究语义、上下文侧栏（contextual inspector）、双层信息密度、验收场景设计
- **OpenCode / Reasonix**：交互品质与终端工作台定位、事件驱动架构思路
- **Kimi / Qwen / Gemini 公开 UI 讨论**：Textual CSS 色板 token、布局分层、状态机与 telemetry footer 设计

明确的弃用项：Nerd Fonts 依赖（终端图标字体不引入，保证零额外字体依赖）。
