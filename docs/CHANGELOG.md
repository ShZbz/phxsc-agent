# PhySc-agent Changelog

版本规则：v0.0.x 为开发期迭代；v0.1.0 起为正式版本。

## v0.1.0 — 2026-08-16 · 首个正式版

- **LLM 请求显式超时**：流式 chunk 60s / 非流式 300s（`PHXSC_LLM_TIMEOUT` / `PHXSC_LLM_STREAM_TIMEOUT` 可覆盖）——修复 SSE stall 导致的"任务卡死、无法中断"；/stop 最长 60s 内必生效
- **命令输入上屏**：/plan、/moa 等斜杠命令的输入回显到聊天区（Tab/点击切换模式仍不上屏）
- **会话自动命名**：首条消息即触发自动命名（原需第 2 条，长任务会话永不满足）
- **persona 锚定**：system prompt 首句加 "You are a helpful software assistant."
- 测试基线 1592 全绿

## v0.0.30 — 2026-08-15 · 正式版前总审修复

- 40 项审计问题全部修复：TUI 会话持久化 / 语义缓存 embedding 异常降级 / 错误上屏 / /moa 并发守卫 / 调度器异常隔离 / CLI 参数默认值修正 / 输入拦截反馈 / thinking·voice 反馈与 header 同步 / resume·fork 聊天区同步 / ModelPicker 输出上屏等
- 测试基线 1564

## v0.0.29 — 2026-08-15 · 启动动画修复

- splash 启动动画四问题修复：动画中断（stop 时机后移）/ 大版错位（终端渲染限制，档1 暂降级为中版动画，三档判定框架保留）/ 双实例守卫 / CRLF 行结束符加固

## v0.0.28 — 2026-08-14 · 交互修复

- 上下文窗口口径修正（DeepSeek V4 为 1M，原 128K 为 V3 旧值）
- 补全框窗口化滚动（选中项保持可视）/ Enter 执行选中命令 / task 触发词治理 / 任务清单行数 = 计划步骤数
- 测试基线 1430

## v0.0.27 — 2026-08-14 · UI 修复

- /new 全量刷新（chat/Inspector/task/Activity/状态栏归零）/ 补全菜单可视区滚动 / 状态栏缓存当前会话口径 / task 源头治理（任务级命名、≤8 步、禁越界）
- 测试基线 1406

## v0.0.26 — 2026-08-14 · 任务可视化

- task 步骤名称显示（从计划文本解析步骤名，中文序号/表格/加粗兜底）/ Inspector 上下分栏
- 测试基线 1351

## v0.0.25 — 2026-08-14 · 长任务修复

- 长任务中断失忆修复（阶段1 执行摘要回写主上下文）/ 补全菜单滚动 / 会话命名约束（防泛标题）
- 测试基线 1333

## v0.0.24 — 2026-08-14 · MoA 多 agent 编排

- **/moa 命令**：主控拆解任务 → N 模型并行（默认 3，上限 4）→ 聚合；SharedSeenSet 线程安全防重复登记；调研/问答/生成三场景（生成落盘 notes/）
- 测试基线 1319

## v0.0.23 — 2026-08-14 · 查重与 AI 降重

- SimHash 查重引擎（blake2b 跨进程稳定）+ 三源对照索引库（PDF 全文/evidence/摘要）
- /dedup 命令 + plagiarism_check / dedup_rewrite 工具（降重仅显式触发，绝不改原文）
- 测试基线 1272

## v0.0.22 — 2026-08-14 · 记忆写入优化

- 三级去重（L1 精确幂等 / L2 语义合并 / L3 merge 预留）/ 重要度自动分级 / 写入门槛（只记偏好·纠正·决策）/ 频控
- 测试基线 1220

## v0.0.21 — 2026-08-13 · 用户实测 12 项修复

- 失忆修复 / Inspector 拉大恢复 / 上下文占用真实口径 / 缓存口径统一 / 网络双通道降级链（proxy→direct→mirror）/ task 动态 label / 上下箭头历史导航 / skill 注入轨 / Ctrl+V 文档路线 / sessions 自动命名等
- 测试基线 1192

## v0.0.20 — 2026-08-13 · UI 审计修复 + 交互层

- UI 独立审计 7 个真实 bug 修复（中断粘滞/并发守卫/组件孤儿/事件发布断链/markup 吞噬/无界增长）
- Command Palette / Session picker / Help / Model picker 四 overlay + STATUS 全量页 + 五档响应式断点
- 测试基线 1171

## v0.0.19 — 2026-08-13 · TUI 重构

- Textual TUI：25 事件 EventBus（线程安全）/ 三模式切换 / 过程可视化（tool 卡片、ACTIVITY 时间线、TaskProgress）/ research 对象视图（gate 流程、paper 卡片、evidence 四态、thinking 折叠）
- 测试基线 1109

## v0.0.18 — 2026-08-12 · 多 provider

- provider 注册表：内置 6 provider（deepseek/zhipu 实测 + openai/anthropic/kimi/mimo 模板）+ 自定义 providers.json；**key 只存环境变量名，永不明文入配置**
- thinking 双形态注入（budget_tokens / reasoning_effort）/ 缓存盐隔离 / /provider /model 命令
- 测试基线 1034

## v0.0.17 — 2026-08-12 · 引用溯源闸门前缀化

- /gate <问题> 请求级触发（替代全局开关）；gate 轮缓存盐隔离（普通轮缓存不被 gate 轮复用）

## v0.0.16 — 2026-08-12 · 脉络追踪 + 会话管理 + 视觉 + PDF 导出

- lineage 研究脉络追踪（OpenAlex 引用网络）+ 交互式 HTML 可视化
- 会话管理：/sessions /search /resume /fork /model /stop
- figure_analyze 视觉接入（tesseract OCR + GLM-4V 兜底 + evidence 入库）
- skill 全文按需加载 / typeset_pdf 导出（Markdown → LaTeX → PDF）/ PPT 审美升级（单 accent 锁定）
- 测试基线 977

## v0.0.15 — 2026-08-12 · 审计修复

- 28 条问题全部修复：会话砖块回滚 / 闸门缓存绕过 / 实体守卫 CJK 边界 / MCP 消息过滤 / 上下文压缩接入 / 检索参数钳制等
- 测试基线 851

## v0.0.14 — 2026-08-11 · MCP 客户端

- MCP 客户端：stdio + HTTP（Streamable HTTP 简化子集）双 transport / 配置解析容错 / 工具动态注册（mcp_<server>_<工具名>）/ /mcp list|status
- 测试基线 763

## v0.0.13 — 2026-08-11 · skill 体系

- SKILL.md 扫描/解析/加载（对齐 agentskills.io 开放标准，兼容 Claude Code / Codex 目录）；元数据表进 system prompt（不破前缀缓存）；skill_load 工具；/skill list|load|unload
- 测试基线 735

## v0.0.12 — 2026-08-11 · 语义缓存 + 混合检索

- SemanticCache（query embedding 桶内余弦 top1 ≥ 0.93 + LRU 500）
- **实体差异守卫**：difflib 检测差异片段中的化学式/年份/数值/型号/ID → 含实体强制 miss（防同形不同义污染缓存）
- 记忆库混合检索（FTS5 BM25 ∪ 向量 → RRF 融合）/ /cache 命令 / telemetry 三口径
- 测试基线 700

## v0.0.11 — 2026-08-11 · thinking 持久化

- /thinking 默认档改 high + 档位持久化（settings.json，重启恢复）

## v0.0.10 — 2026-08-11 · thinking 四档

- off | low | medium | high 四档（DeepSeek budget_tokens 2048/8192/32768）

## v0.0.9 — 2026-08-11 · Sci-Hub 三级保底链

- sci-net.xyz 直连 → sci-hub.ru altcha 协议直解 → 图形 Chrome 兜底；`PHXSC_CHROME_PATH` / `PHXSC_SCI_HUB_MIRROR` / `PHXSC_CDP_PORT` 外部接口

## v0.0.8 — 2026-08-11 · OA 兜底下载

- OpenAlex OA 直链查询 + 下载（魔数校验 + 原子保存）

## v0.0.7 — 2026-08-11 · voice 两档

- /voice academic|natural（去 AI 味）；cache key voice 隔离

## v0.0.6 — 2026-08-11 · 闸门加严

- 引用溯源审核加严：每个事实性论断必须对应证据片段（寒暄/问候豁免）

## v0.0.5 — 2026-08-11 · Tavily 搜索

- web_search_api 工具（Tavily API，免费 1000 次/月，正文级摘要）

## v0.0.4 — 2026-08-11 · web 搜索

- web_search 工具（DuckDuckGo html 端点，零 key 零成本，纯 stdlib 解析）

## v0.0.3 — 2026-08-11 · thinking 开关

- /thinking 开关 + provider 抽象层（意图层 + 翻译层）+ reasoning_content 回传

## v0.0.2 — 2026-08-10 · 单上下文架构

- prompt_toolkit 输入层 + 状态栏 + 方案拷问 + 跨模式上下文 + PPT 三主题
- **单上下文常驻架构**：切模式永不丢上下文 + 前缀缓存零 miss

## v0.0.1 — 2026-08-10 · Day 1-3 基础版

- 骨架 + 沙箱 + 工具注册器 + 三模式（plan/investigate/typeset）+ 四区上下文 + 裸 ReAct loop + arXiv 检索 + CLI
- 记忆层（important 注入轨）+ evidence 库 + 笔记 + 缓存（exact + embed）+ 论文下载 + Zotero 查询 + 引用溯源闸门 + 定向阅读
- 定时任务 + 长任务 plan-then-execute 两阶段 + Markdown → PPTX + telemetry
- 336 tests · 30 commits
