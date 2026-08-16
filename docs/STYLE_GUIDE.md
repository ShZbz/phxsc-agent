# PhySc-agent 版式系统规范（STYLE_GUIDE）

> 2026-08-12 · Day 11 落地 · 一次设计，PDF/PPT 两处受益
> 依据：visual-design-system（CRAP/字号梯度/色板/间距网格）+ nature-paper2ppt 学术版式
> + huashu-design / ppt-master / design-taste-frontend 审美规格（单 accent 锁定、反 AI slop）

## 1. 核心原则

1. **单 accent 锁定**：每个主题只有一个强调色系，accent 与 primary 同族同色。禁止蓝+红、红+绿双强调。
2. **不纯黑纯白**：正文用深灰（非 #000000），浅底用暖白/冷白；深色主题背景用 off-black 微暖深灰
   （避免纯 GitHub 深蓝黑 #0D1117 家族）。
3. **反 AI slop**：不新增装饰性元素（禁 emoji 图标、禁渐变堆砌、禁每段配 icon）；每个元素必须
   earn its place。
4. **强调方式**：同族加粗（标题 Bold）+ primary 色。不在装饰上引入第二色。
5. **形状一致锁定**：全直角（不混圆角矩形），圆角只用于特殊声明元素（若有需统一规则）。
6. **克制密度**：少容器少 border，给内容留气口；"一个细节做到 120%，其他做到 80%"。

## 2. 三主题色板（THEMES，typeset.py 固化）

| 键 | academic（学术蓝） | deep（深色科技） | warm（暖色简约） |
|----|-------------------|------------------|------------------|
| primary | `1F4E79` | `0D9488` | `B85042` |
| secondary | `E8EEF7` | `2A3440` | `E7E8D1` |
| accent | `1F4E79`（=primary） | `0D9488`（=primary） | `B85042`（=primary） |
| background | `FFFFFF` | `1E242B`（微暖深灰） | `FAF6F0` |
| text | `333333` | `F1F5F9` | `333333` |
| code_bg | `F1F5F9` | `1A2333`（深色底） | `F4EFE6`（暖浅底） |

- 渲染一律从 theme 取色，禁止硬编码（发现硬编码 → 改走 theme）
- deep 主题代码块文字色必须用 `theme["text"]`（深底浅字，可读性）

## 3. 字体与字号梯度

| 元素 | 字体 | 字号 |
|------|------|------|
| 封面标题 | 微软雅黑 **Bold** | 40pt |
| 章节标题 | 微软雅黑 **Bold** | 36pt |
| 内容页眉 | 微软雅黑（regular） | 12pt |
| 正文 | 微软雅黑（regular） | 15pt |
| 表格 | 微软雅黑（regular） | 12-14pt |
| 代码块 | Consolas / 等宽 | 11pt |

- 西文数字走 Calibri（现有 `_set_font` latin 参数）
- 层级表达用字号梯度 + 字重（Bold），不靠颜色堆砌

## 4. 布局与间距

- 16:9 宽屏；边距 ≥0.5in；行距 1.3
- 每内容页上限：6 段落块 / 8 列表项 / 1500 字符（防溢出）
- 封面：大标题 + 副标题（来源与更新时间）+ 留白（不堆装饰）
- 章节页：左侧 primary 色条 + 编号 + 标题
- 内容页：页眉 primary 色 12pt；正文 text 色
- 表格：表头 primary 底白字；隔行 secondary；细边框（不圆角）
- 代码块：code_bg 底 + 等宽字体；deep 主题深底浅字

## 5. 反 AI slop 检查清单（交付前自检）

- [ ] 全篇只有一个强调色系（accent == primary 同族）？
- [ ] 无 emoji 作图标、无装饰性 icon？
- [ ] 无紫色渐变 / 无纯黑 #000000 正文 / 无 #0D1117 家族背景？
- [ ] 标题同族加粗，未混入第二字体族？
- [ ] 圆角/直角全篇一致？
- [ ] 每页内容密度达标（不空不满）？

## 6. PDF（typeset_pdf）版式

- LaTeX 模板：ctexart 11pt + geometry 2.5cm 边距 + amsmath + hyperref（链接蓝）
- 白底学术风（PDF 不套深色主题，style 参数仅兼容 academic）
- 表格 booktabs 风格（\hline 表头上下）、代码 verbatim、公式 $..$/$$..$$ 原样透传

## 7. 持续打磨（Day n）

审美优化是持续项：每轮打磨聚焦一个维度（色板 → 字体 → 间距 → 版式），
改动必须回归三主题一致性（THEMES 色值有精确断言锁定）。
