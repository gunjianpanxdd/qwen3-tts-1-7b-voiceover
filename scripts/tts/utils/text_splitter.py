"""中文文字稿分句、分块工具。"""
from __future__ import annotations

import re

SENTENCE_END = "。！？；…!?;"
_SENT_RE = re.compile(f"(?<=[{re.escape(SENTENCE_END)}])")


def clean_text(text: str) -> str:
    """去 BOM、去空行、统一换行。"""
    text = text.lstrip("\ufeff")
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def split_sentences(text: str) -> list[str]:
    """按句末标点切句，标点保留在句尾。"""
    text = clean_text(text)
    if not text:
        return []
    parts = _SENT_RE.split(text)
    return [p.strip() for p in parts if p and p.strip()]


def chunk_sentences(
    sentences: list[str], max_chars: int = 300, min_chars: int = 40
) -> list[str]:
    """句子按 max_chars 聚合为块；超长单句硬切；过短的尾块并入前块。"""
    chunks: list[str] = []
    current = ""
    for sent in sentences:
        if len(sent) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            rest = sent
            while len(rest) > max_chars:
                chunks.append(rest[:max_chars])
                rest = rest[max_chars:]
            current = rest
            continue
        if current and len(current) + len(sent) > max_chars:
            chunks.append(current)
            current = sent
        else:
            current += sent
    if current:
        chunks.append(current)
    if len(chunks) >= 2 and len(chunks[-1]) < min_chars:
        chunks[-2] += chunks[-1]
        chunks.pop()
    return chunks


def split_script_with_para(
    text: str, max_chars: int = 300, min_chars: int = 40
) -> tuple[list[str], list[bool]]:
    """文字稿 -> (分块列表, 每块是否为所在段的最后一块)。

    段落信息必须在切块时记住：拼接时段落之间要留更长静音，
    否则口述类稿子（一段一个故事）会像一口气读到底（2026-11-04 用户反馈）。
    """
    raw = (text or "").replace("\r\n", "\n").lstrip("\ufeff")
    paras = re.split(r"\n\s*\n", raw)
    chunks: list[str] = []
    para_ends: list[bool] = []
    for para in paras:
        sents = split_sentences(para)
        if not sents:
            continue
        sub = chunk_sentences(sents, max_chars=max_chars, min_chars=min_chars)
        for i, chunk in enumerate(sub):
            chunks.append(chunk)
            # 段内被切成多块时，只有最后一块算"段落结束"
            para_ends.append(i == len(sub) - 1)
    return chunks, para_ends


def split_script(text: str, max_chars: int = 300, min_chars: int = 40) -> list[str]:
    """文字稿 -> 分块列表（入口，不关心段落）。"""
    return split_script_with_para(text, max_chars=max_chars, min_chars=min_chars)[0]


# 段级标注行：　【第1段】语速：慢(1.35) | 情感：平静+轻微低落
_DIRECTIVE_LINE_RE = re.compile(
    r"(?m)^【\s*第\s*\d+\s*段\s*】[^\n]*"
)


def strip_directives(text: str) -> str:
    """删除所有段级标注行，只留正文（用于不支持标注参数的引擎）。"""
    return _DIRECTIVE_LINE_RE.sub("", text or "").strip()


# Markdown 记号：writer 产出的稿件常带 **加粗** 等格式符号，TTS 引擎不消费格式，
# 原样喂入会在加粗词上叠音并引发块内音色漂移，字幕也会把记号带上屏
_MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s*", re.M)
_MD_CODE_RE = re.compile(r"`([^`]*)`")
_MD_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITALIC_RE = re.compile(r"\*([^*]+)\*")
_MD_LIST_RE = re.compile(r"^\s*[-*+]\s+", re.M)
# 数字列表：ASCII 的 "." / ")" 必须带空格才剥（防误伤行首小数 3.14）；中文"、"无需空格
_MD_NUMLIST_RE = re.compile(r"^\s*\d+[.)]\s+", re.M)
_MD_CN_NUMLIST_RE = re.compile(r"^\s*\d+[、]\s*", re.M)
_MD_QUOTE_RE = re.compile(r"^\s*>\s?", re.M)


def strip_markdown(text: str) -> str:
    """剥掉 Markdown 记号只留正文，供 TTS 输入使用（纯 regex，无外部依赖）。

    背景（2026-10-15 线上实测）：稿件里的「**防御性成熟**」直喂 Qwen3-TTS，
    模型在加粗词上叠音读成「防防御性成熟」，并引发其后约 6-8s 的块内音色漂移
    （听感即"变声"）；result.srt 里 ** 也会原样上屏。humanize.strip_markdown
    行为相同，但 TTS 路径不经过 humanize，故在此独立实现。
    """
    text = text or ""
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = text.replace("→", "，").replace("->", "，").replace("=>", "，")
    text = _MD_LIST_RE.sub("", text)
    text = _MD_NUMLIST_RE.sub("", text)
    text = _MD_CN_NUMLIST_RE.sub("", text)
    text = _MD_QUOTE_RE.sub("", text)
    return text


# ---------- TTS 数字适配（年份口语化） ----------
# Qwen3-TTS 把「2008」按基数词读成「两百零八」，年份必须逐位读「二零零八」。
# 旧实现只认「4 位数字 + 年」紧邻（humanize._TTS_YEAR_RE），漏掉大量真实形态：
#   「1965到1980年」「1981到1996出生」「2008金融危机」（线上实测读成两百零八）。
# 新规则：4 位数字落在年份区间（1840~2199）且后面不跟数量单位时，一律按年份逐位读；
# 范围外（3000年→三千年）与带单位的（3000元 / 15秒 / 2008个人）保持原样按数量读。
# 连接词（到/至/和/-/‑）与普通汉字（金融危机/出生）都判为年份语境。
_TTS_DIGIT_MAP = {
    "0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
    "5": "五", "6": "六", "7": "七", "8": "八", "9": "九",
}
_TTS_AFTER_RE = re.compile(r"(?<!\d)(\d{1,2})(?=后)")
_YEAR_MIN, _YEAR_MAX = 1840, 2199
_YEAR_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
# 数量单位（数字+单位=数量，按基数读）：多字优先，单字兜尾；
# 刻意不含「到/至/和/与/年」——它们是年份连接词/年份标记，不是数量单位
_YEAR_UNIT_RE = re.compile(
    r"\s*(?:分钟|小时|星期|礼拜|季度|号码|平方|立方|公里|千米|厘米|毫米|微米|"
    r"公斤|千克|毫升|美元|美金|英镑|欧元|日元|港元|人民币|块钱|像素|字节|左右|上下|"
    r"以上|以下|以内|以来|[元块钱币分角毛米厘毫微千里丈尺寸公升克斤两吨磅盎司秒钟点刻"
    r"周度届人口个只支条篇首集帧幅张本册部台辆架艘串包箱袋瓶杯碗盘顿趟遍回遭场局组套副"
    r"对双多少余万替亿字词句段])"
)


def _digits_to_cn(s: str) -> str:
    """阿拉伯数字逐位转中文（1965 → 一九六五），不做数量级换算。"""
    return "".join(_TTS_DIGIT_MAP.get(c, c) for c in s)


def _year_repl(m: "re.Match") -> str:
    n = int(m.group(1))
    if not (_YEAR_MIN <= n <= _YEAR_MAX):
        return m.group(0)
    if _YEAR_UNIT_RE.match(m.string, m.end()):
        return m.group(0)
    return _digits_to_cn(m.group(1))


def tts_normalize_numbers(text: str) -> str:
    """年份/代际数字口语化（幂等：已转中文的不受二次影响）。

    - 4 位年份（区间内、不跟数量单位）逐位读：2008金融危机→二零零八金融危机
    - xx后 逐位读：90后→九零后
    - 其余保持原样：3000元、15秒、3000年（范围外按数量读三千年）
    """
    if not text:
        return text
    text = _YEAR_RE.sub(_year_repl, text)
    text = _TTS_AFTER_RE.sub(lambda m: _digits_to_cn(m.group(1)), text)
    return text


def tts_normalize_hits(text: str) -> int:
    """统计 tts_normalize_numbers 会改写的数字字符数（日志/统计用）。"""
    if not text:
        return 0
    new = tts_normalize_numbers(text)
    return sum(1 for a, b in zip(text, new) if a != b)


def tts_residual_hits(text: str) -> list[str]:
    """扫描仍会被 TTS 按基数读错的数字形态（年份区间内不跟单位的 4 位数字 + xx后）。"""
    if not text:
        return []
    hits = [m.group(0) for m in _YEAR_RE.finditer(text) if _year_repl(m) != m.group(0)]
    hits += [m.group(0) for m in _TTS_AFTER_RE.finditer(text)]
    return hits
