---
name: latex-cjk-pdf
description: 用 LaTeX + xelatex 生成含中文和数学公式的专业 PDF（笔记/讲义/复习资料）。WSL 环境字体坑已验证。当需要把 Markdown 笔记转 PDF、含化学方程式或数学公式排版时使用
version: 0.1.0
---

# LaTeX CJK PDF 生成（xelatex）

## 适用场景

- 学习笔记/知识清单 → PDF（手机/打印），文档含中文字符 + 数学公式（`$$...$$`）或化学方程式
- WSL 环境（xelatex + xeCJK）

## 推荐方案

**不用 pandoc**（模板兼容问题多，xeCJK 与 pandoc 默认模板配合差）。直接写完整 `.tex` 文件，xelatex 编译：

```bash
xelatex 文件名.tex
xelatex 文件名.tex   # 必须编译两次，第二次更新目录/引用/页码
```

## 基础模板

```latex
\documentclass[10pt,a4paper]{article}
\usepackage{xeCJK}
\setCJKmainfont{Source Han Sans CN}   % 见字体表
\usepackage{amsmath,amssymb}
\usepackage{geometry}
\geometry{margin=1.5cm}
\usepackage{fancyhdr}
\setlength{\headheight}{13.6pt}       % 避免 fancyhdr headheight 警告，必须在 \pagestyle 前
\pagestyle{fancy}
\fancyhf{}
\fancyhead[C]{\small 标题}
\fancyfoot[C]{\thepage}
\renewcommand{\headrulewidth}{0.4pt}
\usepackage{xcolor}
\usepackage{hyperref}
\hypersetup{colorlinks=true, linkcolor=blue}

\setlength{\parindent}{0pt}
\setlength{\parskip}{3pt}

\begin{document}
\section{章节标题}
正文内容...

$$ E = \hbar\omega $$
\end{document}
```

## 已验证字体表（WSL + xelatex）⚠️

**核心坑**：WSL 的 xdvipdfmx **不支持 Variable Font**（如 NotoSansSC-VF.ttf 报 `Invalid font: -1`）和部分 TTC 集合。

| 可用 | 类型 | 说明 |
|------|------|------|
| Source Han Sans CN | .ttf | `/mnt/c/Windows/Fonts/SourceHanSansCN-Normal.ttf`，思源黑体，推荐主字体 |
| Adobe Heiti Std | .otf | Windows 字体目录，备选主字体 |
| SimSun / SimHei / FangSong / KaiTi | .ttc/.ttf | `/mnt/c/Windows/Fonts/`，✅ 实测可用 |
| Times New Roman / Courier New | - | ✅ 实测可用 |

| 不可用 | 原因 |
|--------|------|
| Noto Sans SC (VF.ttf) | Variable Font，xdvipdfmx 报 Invalid font: -1 |
| WenQuanYi Zen Hei (.ttc) | TTC 集合格式，可能失败 |

验证命令：`fc-list | grep 字体名`；`fc-match -v "字体名" | grep "file:"`（确认 .otf/.ttf 非 .ttc 非 VF）。新装字体后 `fc-cache -f` 刷新。

## 化学方程式（mhchem v4，材料/物理文档常用）

```latex
\usepackage[version=4]{mhchem}
\ce{H2O} \ce{Ca(OH)2} \ce{Fe3O4}       % 数字直接跟元素后自动下标
\ce{4P + 5O2 -> 2P2O5}                % -> 反应箭头，<=> 可逆箭头
\ce{H+} \ce{OH-}                       % 离子电荷
```

- `\ce{}` 内 `_` 不表示下标（数字自动下标）；`\ce{}` 外 `_` 必须转义 `\_`
- **标题含化学式必须转义下划线**：`\subsection{CO\_2的性质}`（否则 Missing $ inserted）
- ↑↓ 气体/沉淀符号直接写

## 黑体定义（xeCJK 不预定义 \heiti）

```latex
% ❌ \setCJKsansfont{SimHei} 无效，勿用
% ✅ 正确写法：
\setCJKfamilyfont{simhei}{SimHei}
\newcommand{\heiti}{{\CJKfamily{simhei}}}
```

## 特殊 Unicode 符号注册

Unicode 符号（①△★ 等）不在英文主字体中 → 报 Missing character 且不显示。注册为 CJK 字符让 xeCJK 用中文字体渲染：

```latex
\xeCJKDeclareCharClass{CJK}{"2460->"2473}  % 圆圈数字 ①-⑳
\xeCJKDeclareCharClass{CJK}{"25B3}         % △（加热符号）
\xeCJKDeclareCharClass{CJK}{"2605}         % ★
```

## 黑白打印（B&W）

1. 盒子颜色全灰度：`colback=gray!5, colframe=gray!50`（主强调）/ `gray!3, gray!30`（次要）/ `gray!8, gray!50`（推导）
2. 去掉所有 `\color{blue!...}` 彩色，纯黑 `\bfseries`
3. `\hypersetup{colorlinks=false}`
4. 分割线用 `\rule{0.6\textwidth}{0.5pt}` 代替彩色 rule

## 关键 Pitfalls

1. **`\color{blue}` 需显式 `\usepackage{xcolor}`**（hyperref 间接加载不可靠）
2. **`\text{}` 需 `\usepackage{amsmath}`**，否则 Undefined control sequence
3. `.tex` 特殊字符必须转义：`# $ % & ~ _ ^ \ { }`
4. **fancyhdr headheight 警告**：`\setlength{\headheight}{13.6pt}` 必须在 `\pagestyle{fancy}` 之前
5. **两次编译**：首次生成 .aux，第二次才出正确目录/引用/页码
6. **引用格式**（GB/T 7714 上标）：`~\textsuperscript{[1]}`（`~` 是 tie 不可断行空格，防编号单独换行）；连续多篇 `~\textsuperscript{[2-4]}`；国际期刊用内联 `~$[N]$`
7. **编辑 .tex 用整体重写（write_file），不要用 patch 小改**——patch 会把 `\\` 翻倍成 `\\\\` 损坏 LaTeX 命令
8. 摘要等单节居中：不要全局改 `\titleformat{\section}{\centering...}`（所有节标题全居中）；用手动 `{\centering\heiti\fontsize{16}{20}\selectfont 摘\quad 要\par}` + `\addcontentsline`
9. 删除多余 `\newpage`：内容自然结束时紧接 \newpage 会产生大片空白页，让 LaTeX 自动分页

## 替代方案（LaTeX 不可用时）

- WeasyPrint（HTML→PDF）：不支持 LaTeX 公式渲染
- Chrome/Edge headless 打印：支持 MathJax 但设置复杂
