"""图像分析工具：figure_analyze。

本地图片（论文图表/实验照片）→ tesseract OCR 提取文字（chi_sim+eng）；
OCR 文本质量不足（去空白 <20 字符）时自动调用智谱 GLM-4V-Flash 视觉模型
生成图表描述兜底；结果（OCR 文本 + 可选视觉描述）入 evidence 库。

纯 stdlib（subprocess 调系统 tesseract + urllib 调智谱视觉 API），
零新增 Python 依赖。
"""

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from phxsc.agent.tools import tool
from phxsc.sandbox.paths import safe_read_path
from phxsc.tools.memory import _get_store

VISION_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
VISION_MODEL = "glm-4v-flash"
ZHIPU_KEY_ENV = "ZHIPU_API_KEY"
OCR_TIMEOUT = 90
VISION_TIMEOUT = 30
SNIPPET_MAX = 2000
QUALITY_MIN_CHARS = 20
SUPPORTED_EXTS = {"png", "jpg", "jpeg", "webp"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
VISION_PROMPT = (
    "请描述这张图片的学术内容：图表类型、坐标轴含义、关键数据趋势、文字内容。"
    "若包含表格请转述表格内容。保持客观简洁（200 字内）。"
)


class OCRError(RuntimeError):
    """OCR 失败（tesseract 非零退出或超时），message 即用户可读原因。"""


class _VisionError(RuntimeError):
    """视觉模型兜底失败（网络/解析等），message 即失败原因。"""


def _ocr_tesseract(path: str) -> str:
    """tesseract OCR 提取文字（chi_sim+eng），返回 strip 后的纯文本。

    非零退出抛 OCRError（含 stderr 截断 200 字符）；超时抛 OCRError("OCR 超时")。
    """
    try:
        proc = subprocess.run(
            ["tesseract", path, "stdout", "-l", "chi_sim+eng"],
            capture_output=True,
            timeout=OCR_TIMEOUT,
            check=False,
            text=True,
        )
    except subprocess.TimeoutExpired:
        raise OCRError("OCR 超时") from None
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:200]
        detail = f"：{stderr}" if stderr else ""
        raise OCRError(f"tesseract 退出码 {proc.returncode}{detail}")
    return (proc.stdout or "").strip()


def _vision_caption(path: str, ocr_fragment: str) -> str | None:
    """调用智谱 GLM-4V-Flash 生成图片学术描述；无 ZHIPU_API_KEY 返回 None。

    网络/HTTP 错误重试 1 次仍失败抛 _VisionError（调用方记录 note，不整体报错）；
    webp 等智谱可能不支持的格式同样走错误路径。ocr_fragment 作为参考上下文附进 prompt。
    """
    key = os.environ.get(ZHIPU_KEY_ENV)
    if not key:
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise _VisionError(f"读取图片失败：{exc}") from None
    ext = Path(path).suffix.lstrip(".").lower()
    if ext not in SUPPORTED_EXTS:
        ext = "png"
    b64 = base64.b64encode(raw).decode("ascii")
    prompt = VISION_PROMPT
    if ocr_fragment:
        prompt += f"\nOCR 已提取文字片段（供参考）：{ocr_fragment[:200]}"
    body = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/{ext};base64,{b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 1024,
    }
    req = urllib.request.Request(
        VISION_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    last_error = "未知错误"
    for _ in range(2):  # 网络/解析失败重试 1 次
        try:
            with urllib.request.urlopen(req, timeout=VISION_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
            ValueError,
            TypeError,
        ) as exc:
            last_error = str(exc) or type(exc).__name__
            continue
        if not isinstance(payload, dict):
            raise _VisionError("视觉 API 响应不是 JSON 对象")
        choices = payload.get("choices") or []
        content = choices[0].get("message", {}).get("content") if choices else None
        if isinstance(content, str) and content.strip():
            return content.strip()
        raise _VisionError("视觉 API 响应缺少 choices[0].message.content")
    raise _VisionError(last_error)


@tool(
    name="figure_analyze",
    description=(
        "分析论文/实验图片：本地图片路径 → tesseract OCR 提取文字（chi_sim+eng）；"
        "OCR 文本过短时自动调用智谱 GLM-4V-Flash 视觉模型生成图表描述兜底；"
        "结果入 evidence 库。输入为本地图片文件路径（png/jpg/jpeg/webp）。"
        "返回 OCR 文本 + 可选视觉描述 + evidence 记录"
    ),
    mode={"investigate"},
)
def figure_analyze(image_path: str) -> dict:
    """分析本地图片并入库 evidence。返回 {image_path, ocr_text, ocr_quality,
    vision_caption, evidence_id, note}；文件不存在/OCR 失败返回 {error, fix_hint}。"""
    if not os.path.isfile(image_path):
        return {
            "error": f"图片不存在：{image_path}",
            "fix_hint": "检查路径是否正确（支持 png/jpg/jpeg/webp）",
        }
    # 沙箱白名单校验（同 pdf/notes/typeset 工具）：图片须在 workdir 内
    workdir = os.environ.get("PHXSC_WORKDIR") or str(
        Path(__file__).resolve().parents[3] / "workspace"
    )
    try:
        safe_read_path(image_path, workdir)
    except ValueError as exc:
        return {"error": f"图片路径被拒绝：{exc}", "fix_hint": "请使用工作区内（workspace/）的图片路径"}
    try:
        ocr_text = _ocr_tesseract(image_path)
    except OCRError as exc:
        return {
            "error": f"OCR 失败：{exc}",
            "fix_hint": "图片是否损坏或格式不支持？",
        }

    quality = "good" if len("".join(ocr_text.split())) >= QUALITY_MIN_CHARS else "poor"

    caption = None
    reason = ""
    if quality == "poor":
        try:
            caption = _vision_caption(image_path, ocr_text)
        except _VisionError as exc:
            reason = f"视觉兜底失败：{exc}"
        if caption is None and not reason:
            reason = "无 ZHIPU_API_KEY，已降级为纯 OCR"

    snippet_parts = [ocr_text]
    if caption:
        snippet_parts.append(f"[视觉描述] {caption}")
    snippet = "\n".join(part for part in snippet_parts if part)[:SNIPPET_MAX]

    size = os.path.getsize(image_path)
    source_id = f"{os.path.basename(image_path)}@{size}"
    evidence_id = _get_store().add_evidence(source_id, 0, snippet)

    note_parts = []
    if reason:
        note_parts.append(reason)
    elif caption is not None:
        note_parts.append("OCR 文本过短，视觉模型已生成补充描述")
    note_parts.append(f"已写入 evidence #{evidence_id}")
    return {
        "image_path": image_path,
        "ocr_text": ocr_text,
        "ocr_quality": quality,
        "vision_caption": caption,
        "evidence_id": evidence_id,
        "note": "；".join(note_parts),
    }
