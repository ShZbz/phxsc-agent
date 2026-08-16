"""PhySc skill 体系：SKILL.md 扫描/解析 + 元数据表 + 正文加载。

缓存经济学铁律：元数据表只进 system prompt（区1，启动组装一次，字节稳定）；
skill 正文只走 user 消息/工具返回（区2）。任何动态内容不进前缀。
"""
