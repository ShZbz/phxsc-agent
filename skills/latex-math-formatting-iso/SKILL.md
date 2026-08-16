---
name: latex-math-formatting-iso
description: LaTeX 数学公式格式规范（ISO 80000-2:2019 + AMS + SIAM）。当用户要求写含公式的 LaTeX 文档、检查公式排版、或任何数学公式要产出为 PDF/HTML 文档时使用
version: 0.1.0
---

# LaTeX 数学公式格式规范（ISO 80000-2:2019 + AMS + SIAM）

场景分层：**CLI/TUI 聊天**中无需此规范，正常用 Unicode 数学符号即可（无编译环境）。
**产出 PDF/HTML 文档**时才必须遵守以下规范。

---

## 一、字体选用规则（ISO 80000-2:2019 §4.1）

| 元素 | 字体 | 说明 | 示例 |
|------|------|------|------|
| **变量** | 斜体 italic | 会变化的值 | $x$, $y$, $T$, $n$ |
| **数字** | 正体 upright | 数值本身 | 351, 1.32, 7/8 |
| **数学常数** | 正体 upright | 值不变 | e（自然底数）, i（虚数单位）, π |
| **已知函数** | 正体 upright | 标准函数名 | sin, exp, ln, det, log |
| **微分算子 d** | 正体 upright | $\mathrm{d}x$ | |
| **虚数单位 i** | 正体 upright | $\mathrm{i}$ | |
| **自然底数 e** | 正体 upright | $\mathrm{e}^{x}$ | |
| **特殊算子** | 正体 | diag, sgn, tr, rank | |

**核心原则：斜体 = "会变的值"（变量），正体 = "不变的值"（常数、已知函数、算子）。**

## 二、函数参数写法（§4.2）

- 函数符号后直接跟括号，不留空格：`f(x)`, `\cos(\omega t + \phi)`
- 函数名 ≥ 2 字母且参数不含运算符时，可省括号但**必须加细空格**：✅ `\sin\, n\pi`，❌ `\sin n\pi`
- 任何可能混淆处**必须加括号**：`\cos(x) + y`

## 三、分式与斜杠的选择

| 上下文 | 推荐形式 | 原因 |
|--------|---------|------|
| **行内（inline）** | `a/b` 斜杠 | 不破坏行距 |
| **独立公式（display）** | `\frac{a}{b}` | 更清晰 |
| **指数/上标内** | `a/b` 斜杠 | \frac 会过小 |
| **复杂表达式** | 独立公式 `\frac` | 可读性优先 |

斜杠分数的括号必须明确运算顺序：`1/2a + b` 有二义性 → `1/(2a) + b` 或 `(1/2)a + b`。

## 四、公式标点

公式是句子的组成部分，按语法加标点。末尾可以有逗号或句号，取决于在句中的语法位置。不要给每个公式都加逗号。

## 五、括号规范

- 层级递增：`( )` → `[ ]` → `\{ \}`
- `\bigl`/`\bigr` 系列优先于 `\left`/`\right`（间距更佳）
- 函数参数不必用 `\left`：`f(x)` 而非 `f\left(x\right)`

## 六、间距规则

| 命令 | 宽度 | 用途 |
|------|------|------|
| `\,` | 3/18 quad | 微分前、函数后、积分元前 |
| `\:` | 4/18 quad | 中等空格 |
| `\;` | 5/18 quad | 宽空格 |
| `\!` | -3/18 quad | 缩减间距 |
| `\quad` / `\qquad` | 1em / 2em | 段落级间隔 |

微分算子前加细空格：`\int_a^b f(x) \,\mathrm{d}x`。

## 七、公式编号与引用

仅重要的、被引用的公式才编号。编号放圆括号内，右对齐。多行公式在等号处对齐。

## 八、排版工具选择

- ❌ **weasyprint**（HTML→PDF）用于含数学公式的内容——不执行 JavaScript，MathJax 跑不了
- ❌ 用 Unicode 字符（√、π、²、Ω、×、÷）拼凑数学公式
- ✅ **含数学公式 → LaTeX（xelatex）编译**，公式由 TeX 引擎真正渲染（编译工作流见 latex-cjk-pdf skill）
- ✅ **纯文本/网页预览 → HTML + MathJax**
- ✅ 无论 HTML 还是 LaTeX，格式要求一致：变量斜体、常数正体、行内用 `/`、独立用 `\frac`

### 常用宏
```latex
\newcommand{\eu}{\mathrm{e}}       % 自然底数
\newcommand{\ii}{\mathrm{i}}       % 虚数单位
\newcommand{\dd}{\mathrm{d}}       % 微分算子
\newcommand{\ve}[1]{\mathbf{#1}}   % 矢量
\newcommand{\mat}[1]{\mathbf{#1}}  % 矩阵
```

## 九、LaTeX 编译常见陷阱（tcolorbox）

### 9.1 tcolorbox 的括号结构

自定义命令（`\theory{...}`, `\solution{...}`）跨多行时：

- ❌ `\theory{内容标题}` — 一行内立即闭合，后面正文不在 box 内
- ✅ `\theory{%\n\textbf{内容标题}\n...内容...\n}` — 用 `%` 消除首行换行符

错误表现：box 只包了标题，末尾孤立 `}` 导致 "Too many }'s"。

### 9.2 verbatim 环境不能放在 tcolorbox 内

`\begin{verbatim}...\end{verbatim}` **不能**嵌套在 tcolorbox、minipage、或任何命令参数内部。
症状：`! LaTeX Error: \begin{verbatim} on input line X ended by \end{tcb@savebox}`
替代：用 `\texttt` + 手动 `\\` 换行。

## 参考源

- ISO 80000-2:2019 — 数学符号国际标准
- AMS Style Guide & Author Handbook — 美国数学会出版规范
- SIAM Style Manual — 工业与应用数学学会排版手册
- Ellen Swanson — *Mathematics into Type*
