"""研究脉络追踪可视化工具：lineage_view。

以一篇论文为种子（arXiv ID / DOI / 标题自动识别），内部复用 lineage._lineage_track_impl
拿最新引用网络数据（幂等覆盖 JSON），再渲染成单文件交互式 HTML 力导向图
（vis-network CDN），落盘 <workdir>/lineage/<seed_id>.html。

HTML 模板全部内嵌本文件（self-contained），数据经
<script id="lineage-data" type="application/json"> 注入，JSON 串做 "</" → "<\\/" 转义
防标题等字段含 </script> 破坏页面/HTML 注入；所有字段缺失容错显示 "—"，不崩溃。
"""

import html
import json
import os
from string import Template

from phxsc.agent.tools import tool
from phxsc.tools import lineage as lineage_tools

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>研究脉络追踪：$PAGE_TITLE</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; }
  body { background-color: #f8f9fa; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
  #header { position: fixed; top: 0; left: 0; right: 0; height: 56px; background: #fff; box-shadow: 0 1px 4px rgba(0,0,0,.08); display: flex; align-items: center; padding: 0 20px; box-sizing: border-box; z-index: 10; gap: 16px; }
  #header h1 { font-size: 16px; font-weight: 600; margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #header .stats { font-size: 13px; color: #6c757d; white-space: nowrap; }
  #legend { position: fixed; top: 72px; left: 16px; background: #fff; border-radius: 8px; box-shadow: 0 1px 6px rgba(0,0,0,.12); padding: 12px 16px; font-size: 13px; line-height: 2; z-index: 10; }
  #legend .item { display: flex; align-items: center; gap: 8px; }
  .swatch { width: 14px; height: 14px; border-radius: 50%; display: inline-block; flex-shrink: 0; text-align: center; line-height: 14px; font-size: 12px; }
  #panel { position: fixed; top: 72px; right: 16px; width: 300px; max-height: calc(100vh - 96px); overflow: auto; background: #fff; border-radius: 8px; box-shadow: 0 1px 6px rgba(0,0,0,.12); padding: 16px; font-size: 14px; z-index: 10; box-sizing: border-box; }
  #panel h2 { font-size: 15px; margin: 0 0 12px; }
  #panel .row { margin-bottom: 10px; }
  #panel .label { color: #6c757d; font-size: 12px; }
  #network { width: 100%; height: 100vh; }
  #empty-hint { position: fixed; top: 56px; right: 0; left: 0; bottom: 0; display: none; align-items: center; justify-content: center; font-size: 16px; color: #6c757d; z-index: 5; }
  #cdn-hint { position: fixed; top: 56px; left: 0; right: 0; background: #fff3cd; color: #856404; text-align: center; padding: 8px; font-size: 14px; z-index: 20; display: none; }
</style>
</head>
<body>
  <div id="cdn-hint">可视化库加载失败，请联网后刷新</div>
  <div id="header">
    <h1 id="seed-title">$SEED_TITLE</h1>
    <span class="stats">$STATS_SUMMARY</span>
  </div>
  <div id="legend">
    <div class="item"><span class="swatch" style="background:#4A90D9; color:#fff;">★</span>种子论文</div>
    <div class="item"><span class="swatch" style="background:#E67E22;"></span>上游（引用它）</div>
    <div class="item"><span class="swatch" style="background:#2ECC71;"></span>下游（它引用）</div>
    <div class="item"><span class="swatch" style="background:#8E44AD; height: 4px; border-radius: 2px;"></span>关键节点（紫边）</div>
  </div>
  <div id="panel">
    <h2>节点详情</h2>
    <div id="panel-body"></div>
  </div>
  <div id="empty-hint">未获取到引用网络数据（该论文可能极少被引用/引用极少）</div>
  <div id="network"></div>

  <script id="lineage-data" type="application/json">$DATA</script>
  <script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js" onerror="document.getElementById('cdn-hint').style.display='block';"></script>
  <script>
    var data = JSON.parse(document.getElementById('lineage-data').textContent);
    var panelBody = document.getElementById('panel-body');

    function esc(s) {
      return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function relText(rel) {
      return {seed: '种子', upstream: '上游', downstream: '下游'}[rel] || esc(rel || '—');
    }

    function authorsText(authors) {
      var list = authors || [];
      if (!list.length) return '—';
      var text = list.slice(0, 3).join('、');
      if (list.length > 3) text += '等';
      return text;
    }

    function renderPanel(p) {
      var year = (p.year === null || p.year === undefined) ? '—' : p.year;
      var cited = (p.cited_by_count === null || p.cited_by_count === undefined) ? '—' : p.cited_by_count;
      var venue = p.venue || '—';
      panelBody.innerHTML =
        '<div class="row"><b>' + esc(p.title || '—') + '</b></div>' +
        '<div class="row"><span class="label">年份</span><br>' + esc(year) + '</div>' +
        '<div class="row"><span class="label">被引量</span><br>' + esc(cited) + '</div>' +
        '<div class="row"><span class="label">venue</span><br>' + esc(venue) + '</div>' +
        '<div class="row"><span class="label">作者</span><br>' + esc(authorsText(p.authors)) + '</div>' +
        '<div class="row"><span class="label">关系</span><br>' + relText(p.relation) + '</div>' +
        '<div class="row"><span class="label">关键节点</span><br>' + (p.is_key ? '是' : '否') + '</div>';
    }

    renderPanel(data.seed);

    if (data.nodes.length <= 1) {
      document.getElementById('empty-hint').style.display = 'flex';
    } else if (typeof vis === 'undefined') {
      document.getElementById('cdn-hint').style.display = 'block';
    } else {
      var byId = {};
      for (var i = 0; i < data.nodes.length; i++) byId[data.nodes[i].id] = data.nodes[i].payload;
      var network = new vis.Network(document.getElementById('network'), {
        nodes: new vis.DataSet(data.nodes),
        edges: new vis.DataSet(data.edges)
      }, {
        nodes: { margin: { top: 6, right: 6, bottom: 6, left: 6 } },
        edges: { smooth: false },
        interaction: { hover: true, tooltipDelay: 100 }
      });
      network.on('click', function (params) {
        if (params.nodes.length > 0) {
          renderPanel(byId[params.nodes[0]]);
        } else {
          panelBody.innerHTML = '';
        }
      });
    }
  </script>
</body>
</html>
"""


def _text(value) -> str:
    """字段缺失/None/空串 → "—"；否则转 str。"""
    if value is None:
        return "—"
    s = str(value).strip()
    return s if s else "—"


def _html_escape(value) -> str:
    """把要嵌进 HTML 的文本转义（& < > "），防工具提示/标题注入。"""
    return html.escape(str(value), quote=True)


def _truncate(value, n: int) -> str:
    """文本截断到 n 个字符，超出加 …；空/缺失返回 "—"。"""
    s = str(value).strip() if value is not None else ""
    if not s:
        return "—"
    return s if len(s) <= n else s[:n] + "…"


def _authors_text(authors) -> str:
    """作者列表前 3 位 join("、")，多者加 "等"；空/缺失返回 "—"。"""
    if not authors:
        return "—"
    text = "、".join(str(a) for a in list(authors)[:3])
    if len(authors) > 3:
        text += "等"
    return text


def _node_size(cited) -> float:
    """size = 12 + sqrt(cited_by_count)/5，上限 28；非法/缺失引用量按 0。"""
    try:
        c = max(0, int(cited))
    except (TypeError, ValueError):
        c = 0
    return min(28.0, 12.0 + (c ** 0.5) / 5.0)


def _tooltip(node: dict, relation_text: str, is_key: bool = False) -> str:
    """节点悬浮小卡：关键节点前缀 + 标题全名/年份/被引量/venue/作者/关系。"""
    lines = []
    if is_key:
        lines.append("★ 关键节点")
    lines.append(f"<b>{_html_escape(_text(node.get('title')))}</b>")
    lines.append(f"年份：{_html_escape(_text(node.get('year')))}")
    lines.append(f"被引量：{_html_escape(_text(node.get('cited_by_count')))}")
    lines.append(f"venue：{_html_escape(_text(node.get('venue')))}")
    lines.append(f"作者：{_html_escape(_authors_text(node.get('authors')))}")
    lines.append(f"关系：{relation_text}")
    return "<br>".join(lines)


def _seed_node(seed: dict) -> dict:
    """种子节点：蓝星大节点，payload 供右侧信息面板。"""
    payload = {
        "title": seed.get("title"),
        "year": seed.get("year"),
        "cited_by_count": seed.get("cited_by_count"),
        "venue": seed.get("venue"),
        "authors": seed.get("authors") or [],
        "relation": "seed",
        "is_key": False,
    }
    return {
        "id": seed.get("id") or "seed",
        "label": _truncate(seed.get("title"), 20),
        "size": 30,
        "shape": "star",
        "color": "#4A90D9",
        "borderWidth": 3,
        "title": _tooltip(seed, "种子"),
        "payload": payload,
    }


def _work_node(n: dict) -> dict:
    """普通节点：upstream 橙色 / downstream 绿色；关键节点紫边 + 工具提示前缀。"""
    relation = n.get("relation")
    is_up = relation == "upstream"
    is_key = bool(n.get("is_key"))
    payload = {
        "title": n.get("title"),
        "year": n.get("year"),
        "cited_by_count": n.get("cited_by_count"),
        "venue": n.get("venue"),
        "authors": n.get("authors") or [],
        "relation": relation or "",
        "is_key": is_key,
    }
    node = {
        "id": n.get("id"),
        "label": _truncate(n.get("title"), 20),
        "size": _node_size(n.get("cited_by_count")),
        "color": "#E67E22" if is_up else "#2ECC71",
        "title": _tooltip(n, "上游" if is_up else "下游", is_key=is_key),
        "payload": payload,
    }
    if is_key:
        node["borderWidth"] = 4
        node["borderColor"] = "#8E44AD"
    return node


def _edge(e: dict) -> dict:
    """边：from=source, to=target，箭头指向 target；颜色按关系（橙/绿半透明）。"""
    rel = e.get("relation")
    color = "#E67E2288" if rel == "cited_by" else "#2ECC7188"
    return {
        "from": e.get("source"),
        "to": e.get("target"),
        "arrows": {"to": {"enabled": True}},
        "color": {"color": color},
    }


def _stats_summary(stats: dict) -> str:
    """stats → 顶部摘要：节点数/上游数/下游数/年份范围。"""
    total = _text(stats.get("total_nodes"))
    up = _text(stats.get("upstream_count"))
    down = _text(stats.get("downstream_count"))
    ymin = stats.get("year_min")
    ymax = stats.get("year_max")
    if ymin is None and ymax is None:
        years = "—"
    else:
        years = f"{_text(ymin)}-{_text(ymax)}"
    return f"节点 {total} · 上游 {up} · 下游 {down} · 年份 {years}"


def _build_vis_data(data: dict) -> dict:
    """lineage JSON → vis 渲染数据结构（nodes 含 seed，edges，seed payload，stats）。"""
    seed = data.get("seed") or {}
    seed_node = _seed_node(seed)
    nodes = [seed_node]
    for n in data.get("nodes") or []:
        nodes.append(_work_node(n))
    edges = [_edge(e) for e in data.get("edges") or []]
    return {
        "nodes": nodes,
        "edges": edges,
        "seed": seed_node["payload"],
        "stats": data.get("stats") or {},
    }


def _render_html(data: dict) -> str:
    """把 lineage JSON 渲染成完整 HTML 页面（数据注入做 "</" 转义）。"""
    vis = _build_vis_data(data)
    seed = data.get("seed") or {}
    page_title = _html_escape(_truncate(seed.get("title"), 30))
    seed_title = _html_escape(_text(seed.get("title")))
    summary = _html_escape(_stats_summary(data.get("stats") or {}))
    data_json = json.dumps(vis, ensure_ascii=False).replace("</", "<\\/")
    return Template(HTML_TEMPLATE).substitute(
        PAGE_TITLE=page_title,
        SEED_TITLE=seed_title,
        STATS_SUMMARY=summary,
        DATA=data_json,
    )


def _write_atomic(target: str, text: str) -> None:
    """原子写：先写 .tmp 再 rename（已存在则覆盖，幂等）。"""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.rename(tmp, target)


@tool(
    name="lineage_view",
    description="研究脉络追踪可视化：以一篇论文为种子，抓取其引用网络（复用 lineage_track 数据链路）并渲染成交互式 HTML 力导向图（vis-network）落盘 workspace/lineage/。输入 arXiv ID / DOI / 标题均可自动识别。返回 HTML 文件路径 + 网络统计。浏览器打开 HTML 可拖拽/缩放/点击节点查看详情",
    mode="investigate",
)
def lineage_view(seed: str, top_n: int = 10) -> dict:
    """seed 自动识别同 lineage_track；top_n 默认 10 上限 25。内部调 lineage._lineage_track_impl
    拿最新数据（幂等覆盖 JSON），再渲染 HTML。"""
    try:
        summary = lineage_tools._lineage_track_impl(seed, top_n)
    except lineage_tools.LineageError as exc:
        out = {"error": exc.error, "fix_hint": exc.fix_hint}
        if exc.reason:
            out["reason"] = exc.reason
        return out

    seed_id = summary["seed_id"]
    workdir = lineage_tools._workdir()
    json_path = os.path.join(workdir, lineage_tools.LINEAGE_DIR, f"{seed_id}.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return {
            "error": f"可视化生成失败：读取引用网络 JSON 失败：{exc}",
            "fix_hint": "检查 workspace 目录权限后重试",
        }

    target = os.path.join(workdir, lineage_tools.LINEAGE_DIR, f"{seed_id}.html")
    try:
        html_text = _render_html(data)
        _write_atomic(target, html_text)
    except Exception as exc:
        return {
            "error": f"可视化生成失败：{exc}",
            "fix_hint": "检查 workspace 目录权限后重试",
        }

    html_file = os.path.relpath(target, lineage_tools._PROJECT_ROOT)
    return {
        "seed_title": summary["seed_title"],
        "seed_id": seed_id,
        "html_file": html_file,
        "node_count": len(data.get("nodes") or []),
        "edge_count": len(data.get("edges") or []),
        "note": f"HTML 可视化已生成：{html_file}，浏览器打开即可交互查看（拖拽/缩放/点击节点）",
    }
