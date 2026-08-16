---
name: humanizer
description: 去除文本 AI 味——识别并改写 LLM 典型写作模式（16 类英文模式 + 中文特有套路）。当用户要求写工作总结/综述/报告/文章等文本类内容，或怀疑产出"一眼 AI"时使用
version: 0.1.0
---

# 去 AI 味：识别并改写 LLM 典型写作模式

## 使用时机

用户要求写文本类产出（工作总结、综述、报告、文章、邮件），或任何"读起来像 AI 写的"内容需要改写时。

## 工作流

1. **先写完**（正常思考产出内容）
2. **扫一遍下面的模式清单**，逐项对照
3. **改写命中项**：删套话、合并同义反复、恢复主动语态
4. **交付前终检**：破折号扫描 + 词表扫描

---

## 内容模式（Content Patterns）

### 1. 强行拔高意义/地位
**症状词**：stands/serves as, a testament to, vital/crucial/pivotal/key role, underscores/highlights its importance, reflects broader, marking/shaping, focal point, 标志着、见证了、彰显了、书写了
**改法**：删掉意义拔高句，直接陈述事实。
❌ "……标志着区域统计发展中的关键时刻" → ✅ "……成立于 1989 年"

### 2. 硬凹知名度/媒体报道
**症状词**：independent coverage, leading expert, 广受关注、引发热议、多家媒体报道
**改法**：无上下文的引用列表直接删；有真实语境的保留一条。

### 3. 现在分词假深度（-ing 尾巴）
**症状词**：highlighting/underscoring/emphasizing..., ensuring..., reflecting/symbolizing..., contributing to..., showcasing...
**改法**：砍掉 -ing 尾巴，把核心事实留下。
❌ "...symbolizing Texas bluebonnets, reflecting the community's deep connection" → ✅ "...colors meant to evoke Texas bluebonnets"

### 4. 广告腔/营销腔
**症状词**：boasts, vibrant, rich (figurative), breathtaking, stunning, must-visit, 美轮美奂、令人叹为观止、宛如仙境
**改法**：换成中性陈述。
❌ "Nestled within the breathtaking region..." → ✅ "Alamata Raya Kobo is a town in the Gonder region."

### 5. 模糊归因（weasel words）
**症状词**：Industry reports, Observers have cited, Experts argue, 有专家指出、业内人士认为、据报道（无具体来源时）
**改法**：有真实来源就点名，没有就删掉归因，或改写成可查证的表述。

### 6. 模板化"挑战与展望"段
**症状词**：Despite its..., faces several challenges..., 尽管……但仍……、面临着……的挑战、未来展望
**改法**：把具体问题直接列出来，删掉"尽管/但是"的转折包装。

---

## 语言与语法模式（Language Patterns）

### 7. 高频 AI 词汇
**英文**：Actually, additionally, align with, crucial, delve, fostering, garner, highlight, interplay, intricate, key, landscape, pivotal, showcase, tapestry, testament, underscore, vibrant
**中文**：值得注意的是、总而言之、综上所述、进一步、赋能、抓手、打通、闭环、痛点、底座、能力、颗粒度、落地、深度、广度（滥用时）
**改法**：直接删或换普通词。

### 8. 回避系动词（Copula Avoidance）
**症状**：serves as/stands as/represents/boasts/features 代替 is/has
**改法**：恢复最简单的系动词。
❌ "Gallery 825 serves as LAAA's exhibition space" → ✅ "Gallery 825 is LAAA's exhibition space"

### 9. 否定排比 + 尾巴否定
**症状**："Not only...but..."、"It's not just about..., it's..."、句尾的 no guessing/no wasted motion、"不是……而是……"连环
**改法**：改成一句直接陈述。
❌ "It's not just about the beat; it's part of the aggression" → ✅ "The heavy beat adds to the aggressive tone"

### 10. 强行三段式（Rule of Three）
**症状**：凑三的排比——innovation, inspiration, and industry insights
**改法**：有几个说几个，不凑三。

### 11. 优雅变异（同义替换循环）
**症状**：同一事物在一段里换四个称呼（protagonist/main character/central figure/hero）
**改法**：用同一个词，合并句子。
❌ "The protagonist faces many challenges. The main character must overcome obstacles." → ✅ "The protagonist faces many challenges but eventually triumphs."

### 12. 假范围（False Ranges）
**症状**："from X to Y" 套用在不构成量程的对比上（from the Big Bang to the cosmic web）
**改法**：改成并列列举。

### 13. 被动语态 + 无主语碎片
**症状**："No configuration file needed"、"The results are preserved automatically"、被……、使得……得以……
**改法**：恢复主语和主动语态。
❌ "No configuration file needed" → ✅ "You do not need a configuration file"

---

## 风格模式（Style Patterns）

### 14. 破折号（Em Dash）——硬性禁止
**规则**：最终稿**零破折号**（—、–）。破折号是最可靠的 AI 指纹之一，不是"少用"，是"不用"。
替换优先级：句号（拆句）> 逗号 > 冒号 > 括号 > 重写句子。
也要抓带空格的 ` — ` 和双连字符 ` -- `。
**中文场景**：同样适用——AI 爱用"——"做插入语。改成逗号或拆句。
⚠️ 例外：用户提供的写作样本本身大量用破折号 → 匹配样本频率，不强行禁止。

### 15. 机械加粗
**症状**：关键词全部加粗（**OKRs**, **KPIs**, **Business Model Canvas**）
**改法**：正文里只保留真正需要强调的，其余去掉格式。

### 16. 行内标题式列表
**症状**：列表项以加粗冒号标题开头（**Speed**: ... / **Cost**: ...）
**改法**：改成自然语句，或把标题并入句子。

---

## 中文场景速查（英文模式的中文对应）

| 英文模式 | 中文典型表现 |
|---|---|
| 拔高意义 | 标志着、见证了、彰显、书写了新篇章 |
| 模板展望 | 展望未来、在……的道路上、必将 |
| 模糊归因 | 有专家指出、业内人士认为、资料显示 |
| 三段式 | 不仅是……也是……更是…… |
| 被动/无主 | 被赋予、使得、得以实现 |
| 套话开头 | 在当今……的背景下、随着……的不断发展 |

## 终检清单

- [ ] 破折号扫描：稿中无 `—`、`–`、`——`
- [ ] 词表扫描：无"值得注意的是/综上所述/赋能/抓手"等高频 AI 词
- [ ] 每句有主语、主动语态
- [ ] 无凑三排比、无"不仅…而且…"连环
- [ ] 具体事实在，套话删干净
