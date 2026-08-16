"""Telemetry：每轮 LLM 调用的 token / 成本 / 缓存命中统计，追加写入 JSONL。

数据落在 <项目根>/workspace/tmp/telemetry.jsonl（可用 PHXSC_TELEMETRY_PATH
环境变量覆盖，便于测试隔离）。telemetry 是旁路：record 写失败只打印一次警告，
绝不打断对话主流程。成本在读取时（daily_summary）按 MODEL_PRICES 用原始
token 字段重算，prompt 区分 cache_hit / cache_miss 两种单价。
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

MODEL_PRICES = {
    # 美元 / 1M tokens（DeepSeek 官网 2026-08 价格）
    "deepseek-v4-flash": {
        "cache_hit_input": 0.0028,
        "input": 0.14,
        "output": 0.28,
    },
    # 智谱开放平台公示价（2026-08 核，元/百万 tokens）：
    # glm-4.5-air：输入<32K 档 0.8 / 输出短档 2.0（输出>0.2K 跳 6.0）/ 缓存命中 0.16
    # glm-4-flash：老款长期免费（现役免费档为 GLM-4.7-Flash）
    "glm-4.5-air": {
        "cache_hit_input": 0.16,
        "input": 0.8,
        "output": 2.0,
    },
    "glm-4-flash": {
        "cache_hit_input": 0.0,
        "input": 0.0,
        "output": 0.0,
    },
}


def _default_path() -> str:
    """默认路径：<项目根>/workspace/tmp/telemetry.jsonl。"""
    return str(Path(__file__).resolve().parents[2] / "workspace" / "tmp" / "telemetry.jsonl")


class Telemetry:
    """JSONL 追加式 telemetry：record 写一行，daily_summary 读当日统计。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get("PHXSC_TELEMETRY_PATH") or _default_path()
        self._warned = False
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def record(self, entry: dict) -> None:
        """追加一行 JSON（ensure_ascii=False）；写失败静默降级，只警告一次。"""
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001  telemetry 是旁路，绝不打断主流程
            if not self._warned:
                print(f"警告：telemetry 记录失败（{exc}），本次调用未计入", file=sys.stderr)
                self._warned = True

    def daily_summary(self, date: str | None = None) -> dict:
        """当日统计（本地日期 YYYY-MM-DD，默认今天）。无数据返回全 0。

        calls 为"LLM 调用次数"：cache 命中轮（exact/semantic，0 token）不计入。
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        calls = 0
        prompt_tokens = 0
        completion_tokens = 0
        cache_hits = 0
        cache_misses = 0
        semantic_hits = 0
        semantic_misses = 0
        prompt_cache_hit_tokens = 0
        prompt_cache_miss_tokens = 0
        cost = 0.0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(entry, dict) or not entry.get("ts", "").startswith(date):
                        continue
                    if not entry.get("cache_hit"):
                        calls += 1
                    prompt_tokens += entry.get("prompt_tokens", 0) or 0
                    completion_tokens += entry.get("completion_tokens", 0) or 0
                    prompt_cache_hit_tokens += entry.get("prompt_cache_hit_tokens", 0) or 0
                    prompt_cache_miss_tokens += entry.get("prompt_cache_miss_tokens", 0) or 0
                    cost += self._estimate_cost(entry)
                    if entry.get("cache_hit"):
                        cache_hits += 1
                    else:
                        cache_misses += 1
                    if entry.get("semantic_cache_hit"):
                        semantic_hits += 1
                    elif entry.get("semantic_cache_miss"):
                        semantic_misses += 1
        except OSError:
            pass
        total = prompt_tokens + completion_tokens
        hit_rate = cache_hits / (cache_hits + cache_misses) if (cache_hits + cache_misses) else 0.0
        prefix_denom = prompt_cache_hit_tokens + prompt_cache_miss_tokens
        prefix_rate = prompt_cache_hit_tokens / prefix_denom if prefix_denom else 0.0
        semantic_denom = semantic_hits + semantic_misses
        semantic_rate = semantic_hits / semantic_denom if semantic_denom else 0.0
        return {
            "calls": calls,
            "total_prompt_tokens": prompt_tokens,
            "total_completion_tokens": completion_tokens,
            "total_tokens": total,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "cache_hit_rate": hit_rate,
            "semantic_hits": semantic_hits,
            "semantic_misses": semantic_misses,
            "semantic_hit_rate": semantic_rate,
            "prefix_cache_hit_tokens": prompt_cache_hit_tokens,
            "prefix_cache_miss_tokens": prompt_cache_miss_tokens,
            "prefix_cache_hit_rate": prefix_rate,
            "estimated_cost_usd": cost,
        }

    @classmethod
    def _estimate_cost(cls, entry: dict) -> float:
        """按 MODEL_PRICES 估算单行成本（美元）；未知模型按 0。"""
        prices = MODEL_PRICES.get(entry.get("model", ""))
        if not prices:
            return 0.0
        hit = entry.get("prompt_cache_hit_tokens", 0) or 0
        miss = entry.get("prompt_cache_miss_tokens", 0) or 0
        completion = entry.get("completion_tokens", 0) or 0
        return (
            hit / 1e6 * prices["cache_hit_input"]
            + miss / 1e6 * prices["input"]
            + completion / 1e6 * prices["output"]
        )

    @classmethod
    def pricing_for(cls, model: str) -> dict | None:
        """按模型名返回价格字典；未知模型返回 None（界面显示"未定价"）。"""
        return MODEL_PRICES.get(model)

    def close(self) -> None:
        """无资源需要释放，保持接口一致。"""
        return None
