# -*- coding: utf-8 -*-
"""
口播稿拟人化优化：规则清洗 -> LLM 拟人化（三档风格）-> 后处理安全网
参考：Qwen3-TTS-1.7B-Base 拟人化优化完全指南 + 播客旁白优化管线
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv():
    """加载项目根 .env（只填充未设置的环境变量），兼容独立运行与后端 spawn"""
    try:
        env_file = ROOT / ".env"
        if not env_file.exists():
            return
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

# LLM 配置：只认 OpenAI 兼容协议的环境变量，不绑定任何厂商或模型；
# 三者都需由使用者自行设置（BASE_URL 填接口前缀，脚本会拼接 /chat/completions）。
API_KEY = (
    os.environ.get("HUMANIZE_API_KEY")
    or os.environ.get("OPENAI_API_KEY")
    or ""
)
BASE_URL = (
    os.environ.get("HUMANIZE_BASE_URL")
    or os.environ.get("OPENAI_BASE_URL")
    or ""
).rstrip("/")
MODEL = os.environ.get("HUMANIZE_MODEL") or os.environ.get("OPENAI_MODEL") or ""
MAX_TOKENS = int(os.environ.get("HUMANIZE_MAX_TOKENS", "50000") or 50000)

# ===================== 三档风格 =====================
STYLES = {
    "soulful": {
        "name": "深夜电台",
        "pace": "整体节奏放慢，段落之间留出呼吸空间；长句+多逗号自然放缓",
        "pauses": "…… 只用在需要回味、收束或情绪转折处（全篇不超过 6 处）；句尾可留白收束",
        "sentences": "保持原文句子不变：靠省略号和句尾问号制造温柔节奏",
        "punct": "善用 ……；不用破折号 ——；少用 ！；可用 ？ 制造温柔的反问",
        "length": "篇幅允许明显长于原稿（1.2~2.2 倍），深夜电台需要氛围铺垫；但不要超过 2.2 倍",
    },
    "energetic": {
        "name": "激情演讲",
        "pace": "语气紧凑有力，感叹/反问密集（通过标点实现）；避免拖沓（但不得拆句）",
        "pauses": "…… 极少用（全篇不超过 3 处），只在金句后停顿",
        "sentences": "保持原文句子不变：用感叹号/问号提升力度",
        "punct": "善用 ！ 和 ？；短句用 。 收束；避免 …… 和 ——",
        "length": "篇幅为原稿 0.8~1.8 倍（排比/反问会自然加长），不要注水",
    },
    "casual": {
        "name": "朋友聊天",
        "pace": "自然松弛的口语节奏，有张有弛；不追求整齐的句式",
        "pauses": "…… 适度使用（全篇不超过 8 处），只加在像聊天时自然停顿的地方：转折、犹豫、强调；犹豫/卡顿一律用 '……' 表现，不要用 呃/唔/嗯 等语气词",
        "sentences": "保持原文句子不变：用逗号还原说话的自然断句",
        "punct": "只用逗号/句号断句；不用破折号 ——（TTS 读不出稳定停顿）；少用 ！",
        "length": "篇幅为原稿 0.8~1.5 倍，不注水",
    },
}

DEFAULT_STYLE = "casual"

# ===================== 词表与配置 =====================
# 填充词白名单：已停用（不再插入任何填充词/语气词，只做停顿标点）。
# 保留空表以兼容校验/统计逻辑（fidelity 按此剥离、润色量按此计数，均为 0）。
_FILLER_WORDS = []

# 禁用词（黑名单）：句尾语气尾缀与单字语气词，实测听感突兀，LLM 一律不得新增。
# 只禁「新增」——原文自带的这些字必须原样保留（校验一律按相对基线的差集判定）。
_TAIL_WORDS = ["对吧", "是吧", "你说", "是不是", "呗", "嘛", "吧", "啊", "呢", "呀", "哈"]

# 词表按长词优先排序，保证「对吧」不会被「吧」重复计；
# 空词表用永不匹配的模式兜底，避免 "()" 匹配空串造成误计数。
_FILLER_ALT = "|".join(sorted(_FILLER_WORDS, key=len, reverse=True)) or r"(?!x)x"
_TAIL_ALT = "|".join(sorted(_TAIL_WORDS, key=len, reverse=True))
_FILLER_RE = re.compile("(" + _FILLER_ALT + ")")
_TAIL_RE = re.compile("(" + _TAIL_ALT + ")")

# TTS 数字适配（年份口语化）实现已移至 utils/text_splitter.py，
# 由 humanize（优化稿）与 text2voice（生成音频）共用，保证两条路径行为一致
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils.text_splitter import (  # noqa: E402
    tts_normalize_hits,
    tts_normalize_numbers,
    tts_residual_hits,
)

tts_normalize = tts_normalize_numbers  # 兼容旧函数名

_HUMANIZE_CFG_PATH = ROOT / "voice" / "config.yaml"


def _scalar_block(raw, name):
    """解析 voice/config.yaml 顶层 `<name>:` 段的标量键值（不引入 pyyaml）。
    只认 `key: value` 单行；行尾注释剥离。解析失败返回空 dict。"""
    block = re.search(r"^" + re.escape(name) + r":[^\n]*\n((?:[ \t]+[^\n]*\n?)*)", raw, re.M)
    if not block:
        return {}
    out = {}
    for line in block.group(1).split("\n"):
        line = line.split("#", 1)[0]  # 去行尾注释后再解析
        kv = re.match(r"\s*([A-Za-z_]\w*)\s*:\s*(.+?)\s*$", line)
        if kv:
            out[kv.group(1)] = kv.group(2).strip().strip('"').strip("'")
    return out


def _load_humanize_config():
    """极简解析 voice/config.yaml 的 humanize: 段（只取标量键值，避免引入 pyyaml）。
    任何异常都回退默认值，保证脚本脱离配置文件也能独立运行。"""
    cfg = {"tail_forbidden": True, "filler_per_item_cap": 1, "tts_normalize": True}
    try:
        kv = _scalar_block(_HUMANIZE_CFG_PATH.read_text(encoding="utf-8"), "humanize")
    except Exception:
        return cfg
    for key, val in kv.items():
        if key in ("tail_forbidden", "tts_normalize"):
            cfg[key] = val.lower() != "false"
        elif key == "filler_per_item_cap":
            try:
                cfg[key] = max(0, int(val))
            except ValueError:
                pass
    return cfg


_HUMANIZE_CFG = _load_humanize_config()


def _load_breath_config():
    """气口密度硬约束（breath: 段）。任何异常回退默认值，保证脱离配置也能跑。"""
    cfg = {"enabled": False, "max_span": 20, "hard_max": 28, "use_period": False, "para_cap_div": 20}
    try:
        kv = _scalar_block(_HUMANIZE_CFG_PATH.read_text(encoding="utf-8"), "breath")
    except Exception:
        return cfg
    if "enabled" in kv:
        cfg["enabled"] = kv["enabled"].lower() != "false"
    if "use_period" in kv:
        cfg["use_period"] = kv["use_period"].lower() != "false"
    for k in ("max_span", "hard_max", "para_cap_div"):
        if k in kv:
            try:
                cfg[k] = max(1, int(float(kv[k])))
            except ValueError:
                pass
    if cfg["hard_max"] < cfg["max_span"]:
        cfg["hard_max"] = cfg["max_span"]
    return cfg


_BREATH_CFG = _load_breath_config()


def _filler_cap():
    """填充词单品限频：同一个填充词全篇最多允许「新增」几次，来自 config.yaml，默认 1。"""
    try:
        return max(1, int(_HUMANIZE_CFG.get("filler_per_item_cap", 1)))
    except Exception:
        return 1

# ===================== 段级标注（语速/情感） =====================
# 标注行格式：【第1段】语速：慢(1.35) | 情感：平静+轻微低落
# 注意：Qwen3-TTS 1.7B base 不识别段级标注行（text2voice.py 喂模型前已
# strip_directives 剥除，fish/qwen 均不消费标注）。标注逻辑保留代码、默认
# 关闭（with_emotion=False），仅待支持标注的引擎出现后重测。
SPEED_WORDS = ["极慢", "慢", "中慢", "中", "中偏快", "偏快", "快", "极快"]
SPEED_EXAMPLES = "极慢(1.6) 慢(1.35) 中慢(1.2) 中(1.0) 中偏快(0.85) 偏快(0.85) 快(0.65) 极快(0.45)"
EMO_WORDS = ["平静", "自然", "轻松", "温暖", "温馨", "感动", "回忆", "开心", "高兴", "兴奋", "喜悦", "坚定", "励志", "奋斗", "愤怒", "生气", "悲伤", "难过", "低落", "忧郁", "沮丧", "恐惧", "害怕", "紧张", "反感", "厌恶", "惊讶", "惊喜", "无奈", "委屈", "犹豫", "纠结", "遗憾", "愧疚", "心酸", "压抑", "烦躁", "焦虑", "担忧", "释然", "感慨", "振奋", "自豪", "挣扎", "叹息"]
_EMO_WORD_ALT = "|".join(EMO_WORDS)

_DIRECTIVE_LINE_RE = re.compile(
    r"【\s*第\s*\d+\s*段\s*】\s*语速\s*[:：]\s*(?P<speed>[^|｜\n]*?)"
    r"\s*[|｜]\s*情感\s*[:：]\s*(?P<emo>[^\n【]*)"
)


def extract_directives(text):
    """把标注行从 LLM 输出中剥离，返回 (标注行列表, 正文)。"""
    lines = (text or "").split("\n")
    dirs, body = [], []
    for ln in lines:
        if _DIRECTIVE_LINE_RE.search(ln):
            dirs.append(ln.strip())
        else:
            body.append(ln)
    return dirs, "\n".join(body)


def _validate_directives(dirs):
    """标注行合规校验：语速带数字或词表词、情感含词表词，返回合规行数。"""
    ok = 0
    for ln in dirs:
        m = _DIRECTIVE_LINE_RE.search(ln)
        if not m:
            continue
        sp = (m.group("speed") or "").strip()
        if not re.search(r"[\d.]+", sp) and sp not in SPEED_WORDS:
            continue
        if not re.search(_EMO_WORD_ALT, m.group("emo") or ""):
            continue
        ok += 1
    return ok

# ===================== 规则清洗层 =====================
import requests  # noqa: E402

_KEYCAP_DIGITS = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六", "7": "七", "8": "八", "9": "九", "0": "零"}

# 单码点 emoji 映射（值留空=删除；未来可按语义替换成口语词）
_EMOJI_MAP = {
    "🔥": "", "✨": "", "💡": "", "⚠️": "", "⭐": "", "🌟": "", "✅": "", "❌": "",
    "❗": "", "❓": "", "👍": "", "💪": "", "🎯": "", "🚀": "", "📌": "", "💬": "",
    "❤️": "", "💔": "", "😊": "", "🙏": "", "💯": "", "🎉": "", "💥": "", "⏰": "",
}


def _keycap_repl(m):
    return _KEYCAP_DIGITS.get(m.group(1), m.group(1))


def _strip_emoji(text):
    # keycap 序列（1️⃣ 2️⃣ …）转中文数字
    text = re.sub(r"([0-9*])\ufe0f\u20e3", _keycap_repl, text)
    for emoji, repl in _EMOJI_MAP.items():
        text = text.replace(emoji, repl)
    # 其余 emoji（含 ZWJ 序列整体）删除，并清残留变体符/连接符
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\ufe0f\u200d\u20e3]+", "", text)
    return text


def strip_markdown(text):
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = text.replace("→", "，").replace("->", "，").replace("=>", "，")
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+[.、)]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*>\s*", "", text, flags=re.M)
    return text


def normalize_punct(text):
    text = re.sub(r"[，、]{2,}", "，", text)
    text = re.sub(r"[。]{2,}", "。", text)
    text = re.sub(r"[！]{2,}", "！", text)
    text = re.sub(r"[？]{2,}", "？", text)
    # 只压缩标点两侧的空格/制表符，保留换行（段落分隔用于小标题识别）
    text = re.sub(r"[ \t]+([，。！？；：、])", r"\1", text)
    text = re.sub(r"([，。！？；：、])[ \t]+", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def clean_rules(text):
    """规则清洗：emoji/表情、Markdown 符号、标点规整"""
    text = _strip_emoji(text)
    text = strip_markdown(text)
    text = normalize_punct(text)
    return text


# ===================== 小标题改写 =====================
# 书面稿常见分节小标题（"两头死路：抹布式讨喜，毒刺式刺头"、"不好惹自带确定性"），
# 直接朗读会出戏（听众看不到视觉分隔）。在清洗后、拟人化前，把识别出的小标题
# 改写为自然口播过渡句，并作为新的 fidelity 基线（正文仍逐字保留）。
_TITLE_LINE_RE = re.compile(r"^\s*[^。！？\n]{2,40}[：:][^。！？\n]*$")
# 短标题允许行内逗号（如"你身上的犬儒，不是丧，是防御性成熟"），长度放宽到 30；
# 仍要求无句末标点、无冒号，且前后有空行（在 _detect_title_lines 里判断）。
_TITLE_SHORT_RE = re.compile(r"^\s*[^。！？：\n]{4,30}\s*$")
# 行尾是这些标点的行不算标题（多为被换行截断的正文续行，如"…跳来跳去，"）
_TITLE_TAIL_BAD = "，、；,;"


def _detect_title_lines(text):
    """返回需改写的小标题行号（按换行切分，0-based）。
    判定：含全角冒号且不以句末标点结尾的分节标记；或被空行两侧包围的孤立短句（无句末标点）。"""
    lines = text.split("\n")
    out = []
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        if _TITLE_LINE_RE.match(ln):
            out.append(i)
            continue
        prev_blank = i > 0 and lines[i - 1].strip() == ""
        next_blank = i + 1 < len(lines) and lines[i + 1].strip() == ""
        if _TITLE_SHORT_RE.match(ln) and prev_blank and next_blank and ln.strip()[-1] not in _TITLE_TAIL_BAD:
            out.append(i)
    return out


def _title_rewrite_rule(title):
    """无 API 时的规则兜底：冒号标题拆成'说到这，…。'，普通短句加'接下来看，…。'"""
    s = title.strip()
    if "：" in s or ":" in s:
        s = s.replace("：", "。").replace(":", "。")
        return "说到此，" + s
    return "接下来看，" + s + "。"


def _title_rewrite_llm(title):
    try:
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": "你是口播稿编辑：把分节小标题改写成一句自然口播过渡句，保留全部信息，不要标题感，不要加新观点，句末用句号。"},
                {"role": "user", "content": "改写这一句：" + title},
            ],
            "temperature": 0,
            "max_tokens": 20000,
            "stream": False,
        }
        resp = requests.post(
            BASE_URL + "/chat/completions",
            headers={"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"},
            json=payload, timeout=120,
        )
        resp.raise_for_status()
        c = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        c = c.strip().strip("“”\"'")
        if c and len(c) <= len(title) * 4 + 20:
            return c
    except Exception:
        pass
    return _title_rewrite_rule(title)


def normalize_section_titles(text, api_key=""):
    """把识别出的小标题行改写为口播过渡句；无 API key 用规则兜底。"""
    lines = text.split("\n")
    idxs = _detect_title_lines(text)
    if not idxs:
        return text
    for i in idxs:
        lines[i] = _title_rewrite_llm(lines[i]) if api_key else _title_rewrite_rule(lines[i])
    return "\n".join(lines)

# ===================== 规则兜底 / 后处理 =====================
# 兜底原则：只保留效果确定、位置可预期、不会翻车的操作。
# 砍掉强调重复（复制词性不可控，1.7B 易念成口吃）与思路切换（需语义判断，纯规则做不到）。

_PUNCT_SET = set("，。？！、；：\u201c\u201d\u2018\u2019（）()……——\n")
# 长区间内的分句起始连词：在这些词【之前】插逗号，是中文标准停顿位置。
# 只用双字及以上的连词：单字（就/又/还/才/而）大多出现在"我就知道/他又来了"这类
# 短语内部，断开会拆坏语义。
# 反面教训：早期版本在"了/和/与/及"【之后】插逗号，实测触发 2 次全错——
# "你和，一个人"（和是并列连词）、"读了，这么多年书"（了后必接宾语/数量/补语）。
_COMMA_BREAK_BEFORE = (
    "但是", "然而", "所以", "因此", "因为", "如果", "虽然", "不过", "而且",
    "并且", "于是", "然后", "接着", "最后", "后来", "等到", "直到", "甚至",
    "尤其", "比如", "例如", "换句话说", "也就是说", "总之", "反过来",
    "另一方面", "同时", "另外", "到底", "其实", "原来", "哪怕", "即使",
    "无论", "不管", "只要", "只有", "除非", "一旦", "随后", "从而",
)
_COMMA_BREAK_MAX_LEN = max(len(w) for w in _COMMA_BREAK_BEFORE)


def _insert_comma_long_span(text, threshold=18):
    """长句熔断（防 TTS 赶读）：连续超过 threshold 字无标点的区间，
    在中点附近找分句起始连词（但是/所以/因为/到底…），在其【之前】插逗号。
    找不到连词就放弃该区间（宁缺毋滥，不硬切）。不限次数。"""
    result = list(text)
    i = 0
    while i < len(result):
        if result[i] in _PUNCT_SET:
            i += 1
            continue
        start = i
        while i < len(result) and result[i] not in _PUNCT_SET:
            i += 1
        span_len = i - start
        if span_len <= threshold:
            continue
        mid = start + span_len // 2
        best = None
        for offset in range(span_len // 2):
            for pos in (mid + offset, mid - offset):
                if not (start < pos < i):
                    continue
                head = "".join(result[pos:min(pos + _COMMA_BREAK_MAX_LEN, i)])
                if any(head.startswith(w) for w in _COMMA_BREAK_BEFORE):
                    best = pos
                    break
            if best is not None:
                break
        if best is None:
            continue
        result.insert(best, "，")
        i = best + 1  # 从新逗号之后重扫，保证尾部区间也达标
    return "".join(result)


# ===== 气口密度硬约束（breath guard）=====
# 实测依据（1.7B，同字不同标点、双 seed 受控对照）：Qwen3-TTS 对「，」与「。」
# 给出的停顿几乎等长（0.44s vs 0.44~0.56s），所以「逗号→句号」买不到节奏改善；
# 真正决定听感语速的是「气口密度」——每几个字出现一个停顿。
# 故此处只做一件事：把**离群的长气口**（超长无标点区间）收口到阈值内，
# 不做全篇加密（全篇加密会让听感变碎、时长明显拉长，且 p50 已经达标）。
# 铁律：只插入标点，绝不增删改任何一个字；找不到安全断点就放行（宁缺毋滥）。

# 断在其【之前】会拆坏语义的字：助词/语气词/单字虚词/量词（「的，」「就，」「和，」「个，」）
_BREATH_NO_BREAK_BEFORE = set(
    "的了着过得很太最就都也还才又再被把让给对从向往在于和与及或而但却并吗呢吧啊呀嘛地之"
    "个只条张本件种些座辆台部篇段句位名份批组套层类支根颗朵头匹"
)
# 断在其【之后】会拆坏语义的字：左侧需要一个右邻成分才成立（「和，你」「的，人」）
# 含指示/全称修饰语：后面必须紧跟名词（「那，一刻」「整，个」都是错的）
# 含介词/伴随词：后面必须紧跟宾语（「跟，你说」「为，什么」都是错的）
_BREATH_NO_BREAK_AFTER = set(
    "的了着过得很太最被把让给对从向往在于和与及或而但却并地之这那每各某整全半几"
    "跟比替朝随连同由据按经除归管冲沿顺"
)
# 引号/括号成对符号：内部一律不断（括号类已在 _PUNCT_SET 里天然成段，书名号在这里兜住）
_BREATH_QUOTE_PAIRS = {"“": "”", "‘": "’", "（": "）", "(": ")", "《": "》", "【": "】", "「": "」"}
# 数字与单位之间不断（「一千二，百三十四」「3000，元」）
_BREATH_DIGIT_UNIT = set("元年月日号个位次秒分钟块元角万亿千百%％")
# 汉字数字（str.isdigit 对「一/十/百」为 False，必须显式列出）
_BREATH_CN_NUM = set("零〇一二三四五六七八九十百千万亿两")
# 这些符号之后紧跟的内容是「整体单元」：引号/括号/书名号内部一律不切
_BREATH_UNIT_OPEN = set("“”‘’（()【「『《")
# 断在其【之前】的高精度分句词（补充 _COMMA_BREAK_BEFORE，实测中文口语里最自然的断点）
_BREATH_BREAK_BEFORE_EXTRA = ("不是", "而是", "把", "被", "让")
# 断在其【之后】的小句收尾词（「…的时候，」「…之后，」后必可断）
_BREATH_BREAK_AFTER = ("的时候", "之后", "之前", "以后", "以前", "的话", "来讲", "来说", "来看", "而言")
# 新分句起始的指示词/人称词：只作次级候选（「这/你/我」开头多为新分句）
_BREATH_CLAUSE_HEADS = set("这那你我他她它")
# 视作断句边界、但不参与「气口」统计的符号：Markdown 分隔线/表格/链接等
_BREATH_EXTRA_BOUNDARY = set("=-_*|+~`#&%^<>")


def _breath_seps():
    """气口扫描用的分隔符集合 = 常规标点 + Markdown/ASCII 结构符号。"""
    return _PUNCT_SET | _BREATH_EXTRA_BOUNDARY


def _breath_blocked(span):
    """返回 (hard, soft) 两个等长布尔数组。
    pos 的语义是「在 span[pos] 之前断」，即切成 span[:pos] 与 span[pos:]。
    hard：绝对不许断（引号内、数字/单位内、英文缩写内）。
    soft：默认不许断，但命中「但是/所以/的时候」这类完整语言学信号时可以覆盖
          （单字「但」不许断，但「但是」是一个完整连词，可以断）。"""
    n = len(span)
    hard = [False] * n
    soft = [False] * n
    # 1) 引号/括号内部（含两端符号本身）
    stack = []
    for i, ch in enumerate(span):
        if ch in _BREATH_QUOTE_PAIRS:
            stack.append(_BREATH_QUOTE_PAIRS[ch])
            hard[i] = True
        elif stack and ch == stack[-1]:
            hard[i] = True
            stack.pop()
        elif stack:
            hard[i] = True
    # 2) 数字串内部、数字与单位之间、英文缩写内部
    for i in range(1, n):
        a, b = span[i - 1], span[i]
        a_num = a.isdigit() or a in _BREATH_CN_NUM
        b_num = b.isdigit() or b in _BREATH_CN_NUM
        if a_num and (b_num or b in _BREATH_DIGIT_UNIT):
            hard[i] = True
        if a.isascii() and a.isalnum() and b.isascii() and b.isalnum():
            hard[i] = True  # App、PMF、iPhone
    # 3) 功能字/量词：右侧以虚词开头、或左侧以虚词结尾，都不许断
    for i in range(n):
        if span[i] in _BREATH_NO_BREAK_BEFORE:
            soft[i] = True
        if i + 1 < n and span[i] in _BREATH_NO_BREAK_AFTER:
            soft[i + 1] = True
    return hard, soft


def _breath_pick(span, masks, max_span, hard_max, strict=False):
    """在超长区间里选一个断点；返回 span 内下标，找不到返回 None。
    只会断在**有语言学信号**的位置（连词前 / 小句收尾词后 / 新分句起始词前），
    不做无信号盲切——实测盲切会切坏专名与量词（「克利，夫兰」「那一，刻」），
    收益远小于风险，故宁可放行。
    strict=True 或区间超过 hard_max 时，把搜索范围从「35%~88%」放宽到「20%~88%」。"""
    n = len(span)
    hard, soft = masks
    hi = min(n - 5, n * 88 // 100)  # 右侧至少留 5 字
    wide = strict or n > hard_max
    lo = max(6, n * (20 if wide else 35) // 100)  # 左侧至少留 6 字，避免切出碎句
    if hi <= lo:
        return None

    def ok(p, allow_soft=False):
        if not (lo <= p <= hi) or hard[p]:
            return False
        return allow_soft or not soft[p]

    # 1) 连词/分句起始词之前（中文最标准的停顿位）：完整连词可覆盖单字软禁
    for p in range(lo, hi + 1):
        if not ok(p, allow_soft=True):
            continue
        head = span[p:p + _COMMA_BREAK_MAX_LEN]
        if any(head.startswith(w) for w in _COMMA_BREAK_BEFORE):
            return p
        if any(head.startswith(w) for w in _BREATH_BREAK_BEFORE_EXTRA):
            return p
    # 2) 小句收尾词之后（「…的时候，」「…之后，」）
    for p in range(lo, hi + 1):
        if not ok(p, allow_soft=True):
            continue
        tail = span[max(0, p - 4):p]
        if any(tail.endswith(w) for w in _BREATH_BREAK_AFTER):
            return p
    # 3) 新分句起始词之前——只在区间够长（> hard_max 的一半）且前方无同位语风险时用。
    #    实测这条最容易切坏同位语（「结构性共性，这个工具」），故大幅收紧：
    #    只有区间已经很长、前两级都没命中时才启用，且必须落在后半段。
    halfway = max(lo, n // 2)
    if n >= hard_max * 3 // 4:
        for p in range(halfway, hi + 1):
            if ok(p) and span[p] in _BREATH_CLAUSE_HEADS:
                return p
    return None


def _enforce_max_span(text, max_span=20, hard_max=28, use_period=False, para_cap_div=20, strict=False):
    """气口密度硬收口：把超过 max_span 字的无标点区间断开（默认插逗号）。
    只插入标点，一个字都不动；每段新增上限 = 段字数 / para_cap_div，防止全篇变碎。
    找不到安全断点（全是引号/数字/虚词边界）就放行，不做破坏性硬切。"""
    mark = "。" if use_period else "，"
    seps = _breath_seps()
    lines = []
    for para in text.split("\n"):
        if not para:
            lines.append(para)
            continue
        cap = max(1, len(para) // max(1, para_cap_div))
        result = list(para)
        i = 0
        used = 0
        while i < len(result) and used < cap:
            if result[i] in seps:
                i += 1
                continue
            start = i
            while i < len(result) and result[i] not in seps:
                i += 1
            span = "".join(result[start:i])
            if len(span) <= max_span:
                continue
            # 引号/括号/书名号内部是整体单元（「“…”」「（…）」），一个都不切
            if start > 0 and result[start - 1] in _BREATH_UNIT_OPEN:
                continue
            # 纯符号/ASCII 段（Markdown 分隔线、表格、链接）不切
            if not any("\u4e00" <= c <= "\u9fa5" for c in span):
                continue
            pos = _breath_pick(span, _breath_blocked(span), max_span, hard_max, strict)
            if pos is None:
                continue
            result.insert(start + pos, mark)
            used += 1
            i = start + pos + 1  # 从新标点之后继续扫，保证切完的两段也达标
        lines.append("".join(result))
    return "\n".join(lines)


def breath_stats(text, over=None, sample=4, width=48):
    """气口分布统计：相邻分隔符之间的汉字数（span）。验收「最长气口」是否收敛。
    over 给定阈值时，额外返回 stuck：仍超阈值、且没能被收口的区间样本（诊断用）。"""
    seps = _breath_seps()
    spans, cur, start = [], 0, 0
    stuck = []
    for i, ch in enumerate(text or ""):
        if ch in seps:
            if cur:
                spans.append(cur)
                if over and cur > over and len(stuck) < sample:
                    stuck.append(text[start:i][:width])
                cur = 0
            start = i + 1
        else:
            cur += 1
    if cur:
        spans.append(cur)
        if over and cur > over and len(stuck) < sample:
            stuck.append(text[start:start + width])
    if not spans:
        return {"spans": 0, "maxSpan": 0, "p50": 0, "p90": 0, "p99": 0,
                "over18": 0, "over25": 0, "stuck": stuck}
    ordered = sorted(spans)

    def pct(q):
        return ordered[min(len(ordered) - 1, int(len(ordered) * q))]

    return {
        "spans": len(spans),
        "maxSpan": max(spans),
        "p50": pct(0.5),
        "p90": pct(0.9),
        "p99": pct(0.99),
        "over18": sum(1 for x in spans if x > 18),
        "over25": sum(1 for x in spans if x > 25),
        "stuck": stuck,
    }


_PAUSE_WORDS = ("……",)
_TRANSITION_WORDS = [
    "但是", "然而", "不过", "但", "其实",
    "所以", "也就是说", "换句话说", "说白了",
    "重点是", "关键在于", "没错", "还不止",
    "比如说", "举个例子",
]


def _insert_transition_pauses(text):
    """转折停顿（句首转折词前，≤4 处）：只在句首转折词前插入 '……'。
    纯停顿、不插任何词；长句补逗号在 postprocess 统一执行，此处不重复。"""
    count = 0
    for word in _TRANSITION_WORDS:
        if count >= 4:
            break
        pos = 0
        while True:
            pos = text.find(word, pos)
            if pos == -1:
                break
            if pos == 0 or text[pos - 1] in "。？！\n":
                ctx = text[max(0, pos - 5):pos]
                if not any(m in ctx for m in ("……", "呃", "嗯", "嘶")):
                    text = text[:pos] + random.choice(_PAUSE_WORDS) + text[pos:]
                    count += 1
                    break
            pos += len(word)
    return text


def rule_hesitate(text):
    """纯规则兜底（LLM 不可用或校验全败时）：只在句首转折词前插入停顿（……）。
    不插入任何填充词/语气词/自造词；长句补逗号在 postprocess 统一执行，此处不重复。"""
    return _insert_transition_pauses(text)


def _strip_pronounced_hesitation(text):
    """兜底清除残留的'嗯'/'呃'独立语气词（TTS 念成实读音出戏）。
    只清独立语气词：句首或标点/空白后、后接逗号或结尾的 嗯/呃。"""
    return re.sub(r"(^|[\s，。！？、；：])(嗯|呃)\s*[，,]?", r"\1", text)


def _cap_pauses(text, limit=20):
    parts = text.split("……")
    if len(parts) > limit + 1:
        text = "……".join(parts[: limit + 1]) + "".join(parts[limit + 1:])
    return text


def _limits(n, relax=1.0):
    """按稿长计算省略号停顿上限（约 1/180 字，短稿保底、长稿封顶）。
    relax 为可选的放宽系数。"""
    def cap(v, lo, hi):
        return max(lo, min(hi, v))

    return {
        "pauses": cap(round(max(2, n // 180) * relax), 2, round(10 * relax)),
    }


_PUNCT_NORM = re.compile(r"[，。？！……——、；：\"'\u201c\u201d\u2018\u2019（）()\s,.?!;:]")


def _word_counts(text, words):
    """长词优先、不重叠统计词表出现次数（「对吧」不会被「吧」重复计）。
    空词表直接返回空，避免空 pattern 匹配到每个空串位置。"""
    if not text or not words:
        return {}
    pat = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    counts = {}
    for m in re.finditer(pat, text):
        w = m.group(0)
        counts[w] = counts.get(w, 0) + 1
    return counts


def _new_word_counts(text, baseline, words):
    """只统计「输出相对基线新增」的词：原文自带的词不计入，避免误判。"""
    now = _word_counts(text, words)
    base = _word_counts(baseline, words)
    return {w: n - base.get(w, 0) for w, n in now.items() if n - base.get(w, 0) > 0}


def _tail_violations(text, baseline, enabled=True):
    """禁用词违规：返回新增的句尾语气尾缀/单字语气词 {词: 新增次数}。原文自带的永远不算。"""
    if not enabled:
        return {}
    return _new_word_counts(text, baseline, _TAIL_WORDS)


def _filler_overflow(text, baseline, cap=1):
    """填充词单品超频：返回 {词: 新增次数}，只统计「新增实例数 > cap」的词（cap=0 表示不许新增）。"""
    if cap < 0:
        return {}
    return {w: n for w, n in _new_word_counts(text, baseline, _FILLER_WORDS).items() if n > cap}


def _strip_new_tails(text, baseline):
    """安全网：只删除「相对基线新增」的句尾语气词，原文自带的绝不删。
    只认句末标点前 / 行尾的实例（尾缀的典型位置），并从后往前删，避免误伤原文实例。"""
    extra = _new_word_counts(text, baseline, _TAIL_WORDS)
    for w in sorted(extra, key=len, reverse=True):
        n = extra[w]
        pat = re.compile(r"[，,]?\s*" + re.escape(w) + r"(?=[。！？…，,、；;：:\n]|$)")
        while n > 0:
            hits = list(pat.finditer(text))
            if not hits:
                break
            m = hits[-1]
            text = text[:m.start()] + text[m.end():]
            n -= 1
    return text


def _fidelity_check(text, original):
    """忠实度硬校验：输出去掉标点后，必须与原文（去标点）逐字一致。
    不一致说明 LLM 改写/扩写/删减了原文；边界小差异（全半角、不可见字符）用
    编辑距离兜底放行（≤ 原文 1%）。"""
    def norm(s):
        s = s.replace("*", "")
        # 长词先替换：避免 对吧 -> 单字 吧 被先剥掉后残留 对
        for f in sorted(_FILLER_WORDS, key=len, reverse=True):
            s = s.replace(f, "")
        return _PUNCT_NORM.sub("", s)

    a, b = norm(text), norm(original)
    if a == b:
        return True
    tol = max(1, len(b) // 100)
    return _levenshtein_le(a, b, tol)


def _levenshtein_le(a, b, tol):
    """a、b 编辑距离是否 ≤ tol（按行滚动 dp，行最小距离超 tol 即提前剪枝）。"""
    if len(a) < len(b):
        a, b = b, a
    if len(a) - len(b) > tol:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(v)
            row_min = min(row_min, v)
        if row_min > tol:
            return False
        prev = cur
    return prev[-1] <= tol


def _polish_delta(text, base):
    """润色量：相对清洗稿新增的断句标点/停顿数量；0 表示完全没润色（视为无效输出）。
    注意：省略号按整串（……）计 1 处；这里统计的是"是否真的动手润色了"，
    拟人化只靠插标点实现。"""
    def count(s):
        punct = sum(s.count(p) for p in "，。！？、；：") + s.count("……")
        fillers = len(_FILLER_RE.findall(s))
        return punct + fillers

    return count(text) - count(base)


def _restore_paragraph_breaks(body: str, baseline: str) -> str:
    """LLM 把原稿多个自然段并成一段时，按原稿段落开头把空行补回去。

    依据 samples/比较长篇.md：原稿每段一个空行，优化稿把好几段拼成一大段，
    段落留白（text2voice 的 paragraph_gap_ms）因此失效。
    只在"产物段数明显少于原稿"时动手；正常情况原样返回。
    """
    if not baseline:
        return body
    base_paras = [re.sub(r"\s+", "", p) for p in re.split(r"\n\s*\n", baseline) if p.strip()]
    if len(base_paras) < 2:
        return body
    cur = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    if len(cur) >= len(base_paras):
        return body
    one = re.sub(r"\n\s*\n", " ", body)
    idx = [i for i, ch in enumerate(one) if "\u4e00" <= ch <= "\u9fff"]
    plain = "".join(one[i] for i in idx)
    inserts = []
    for para in base_paras[1:]:
        key = _PUNCT_NORM.sub("", para)[:8]
        if len(key) < 6:
            continue
        pos = plain.find(key)
        if 0 <= pos < len(idx):
            inserts.append(idx[pos])
    if not inserts:
        return body
    for at in sorted(set(inserts), reverse=True):
        one = one[:at] + "\n\n" + one[at:]
    return one


def postprocess(text, keep_tags=False, with_emotion=False, baseline="", tail_forbidden=True):
    dirs, body = extract_directives(text)
    if not with_emotion:
        dirs = []  # 不产情感时，丢弃 LLM 擅自添加的任何标注行
    body = strip_markdown(body)
    """清除 LLM 残留的方括号/花括号标签，规范省略号与标点，控制停顿总量"""
    if not keep_tags:
        body = re.sub(r"[\[【]\s*[^\]】]{0,20}[\]】]", "", body)
    body = re.sub(r"…{2,}", "……", body)
    body = re.sub(r"[，、]{2,}", "，", body)
    body = re.sub(r"—+", "，", body)  # 破折号一律转逗号（已弃用：TTS 读不出稳定停顿）
    if tail_forbidden and baseline:
        body = _strip_new_tails(body, baseline)  # 无基线就无从判断"新增"，保守不动（只清新增尾缀）
    body = _strip_pronounced_hesitation(body)
    body = _cap_pauses(body, limit=_limits(len(body))["pauses"])
    segs = [re.sub(r"\s+", " ", s).strip() for s in re.split(r"\n\s*\n", body) if s.strip()]
    if not dirs:
        out = "\n\n".join(segs)  # 保留段落空行（段落呼吸）；段内换行并入空格
    elif len(segs) == len(dirs):
        # 标注行与正文段一对一配对：标注行紧接对应正文
        out = "\n\n".join(d + "\n" + seg for d, seg in zip(dirs, segs))
    else:
        # 数量不一致：标注行在前、正文在后（引擎按块首标注取参数）
        out = "\n\n".join(dirs) + "\n\n" + "\n".join(segs)
    # 段落被 LLM 合并时，按原稿段落开头把空行补回去（保留段落呼吸）
    if baseline:
        out = _restore_paragraph_breaks(out, baseline)
    # 长句熔断最后兜底（LLM 路径与规则路径统一执行，防任何路径产出赶读长句）
    out = _insert_comma_long_span(out)
    # 气口密度硬收口：兜住连词规则覆盖不到的超长区间（开关见 config.yaml 的 breath: 段）
    if _BREATH_CFG.get("enabled"):
        out = _enforce_max_span(
            out,
            max_span=int(_BREATH_CFG.get("max_span", 20)),
            hard_max=int(_BREATH_CFG.get("hard_max", 28)),
            use_period=bool(_BREATH_CFG.get("use_period", False)),
            para_cap_div=int(_BREATH_CFG.get("para_cap_div", 20)),
            strict=bool(_BREATH_CFG.get("strict", False)),
        )
    # 句首孤立逗号「。 ，然后」「。 ，所以」：LLM 在连词前插标点、连词又落在新句开头时会出现。
    # 放在最后兜底，无论哪一层产生的都清掉；正常中文逗号前不会有空白/句末标点。
    out = re.sub(r"(?<=[。！？!?…\s])，(?=[^\s，。！？])", "", out)
    return out

# ===================== LLM 拟人化 =====================
def build_prompt(text, style="casual", experimental_tags=False, with_emotion=False, relax=1.0):
    st = STYLES.get(style) or STYLES[DEFAULT_STYLE]
    lim = _limits(len(text), relax)
    lines = [
        "你是一位中文口播稿拟人化编辑，专门为 Qwen3-TTS 配音准备朗读稿。",
        "",
        "【任务】对下面的口播稿做标点润色：在原文基础上，只插入标点符号（含空行）来控制停顿，模拟真人说话节奏。除标点外，一个字都不许改。风格：「" + st["name"] + "」",
        "",
        "【允许的操作（只做一件事）】",
        "- 插入标点控制停顿：逗号=微停顿；句号=自然气口；问号=句尾上扬；感叹号=力度增强；省略号……=欲言又止/情绪收束/留余味；空行=段落呼吸",
        "",
        "【严格禁止】",
        "- 禁止插入任何口语填充词或语气词（那个/就是说/哎/唉/你看/说实话/其实/你知道吗/你想想 等一律不许），也不要加 嗯/呃/唔 等鼻音语气词——TTS 会念成实读音，严重出戏；需要犹豫/停顿时只用 '……'。",
        "- 禁止在句尾添加任何语气尾缀或语气词（实测听感突兀，一个都不许加）：" + "、".join(_TAIL_WORDS) + "。",
        "- 不改写任何句子：不拆句、不合并、不换词、不调整语序、不把陈述句改成问句、不加自问自答、不加前置短句（你知道吗？/我告诉你）",
        "- 全篇第一句（开场句）必须原样保留，禁止在句首添加任何起手词",
        "- 输出中的每个汉字都必须与原文完全一致：不能换字、加字、删字，只能插入标点符号（含空行）",
        "- 不添加原文没有的内容：不解释、不转场、不总结、不升华、不加新观点",
        "- 不删除原文任何内容",
        "- 原稿的空行分段必须原样保留：不得删空行、不得把两个自然段并成一段（段落之间要给 TTS 留白）",
        "- 禁止使用破折号 ——：TTS 读不出稳定停顿，一律改用逗号或句号（原文自带破折号时也换成逗号）。",
        "",
        "【风格参考】",
        "- 节奏：" + st["pace"],
        "- 停顿：" + st["pauses"],
        "- 标点：" + st["punct"],
        "- 句式：" + st["sentences"],
        "",
        "【通用限制】",
        "1. 停顿必须加在语义自然的位置：观点转折、重点强调、情绪收束处；逗号要克制，不要每句都插、也不要把一个分句切得太碎；禁止机械添加——每个列举项前都插、每句都插，均不允许。",
        f"2. 全篇克制：省略号停顿不超过 {lim['pauses']} 处；同一句话不要重复强调两遍以上。",
        "3. 禁止出现任何方括号或花括号标签，例如 [停顿]、[轻声]、{强调} 一律不允许；不要使用 emoji、表情符号或任何 Markdown 符号。",
        "4. 如果原文已经很口语且顺口，可以基本不改，只做轻微调整。",
        "",
    ]
    if with_emotion:
        # 注意：Qwen3-TTS 1.7B base 不识别段级标注行，此分支保留仅供未来支持标注的引擎
        lines += [
            "【分段标注（必须）】",
            "- 把润色后的全文按语义分成 2~6 段，每段 2~4 句；每段开头独占一行输出标注：",
            "  【第N段】语速：词(数字) | 情感：词+词",
            "- 语速：从下列词表选一个，括号内数字必须与词一致（数字大于 1 表示慢、小于 1 表示快、1.0 正常）：" + SPEED_EXAMPLES,
            "- 情感：从下列词表选 1~2 个，用 + 连接（如 平静+轻微低落、悲伤+低落、开心+惊喜）：" + " ".join(EMO_WORDS),
            "- 情感词前可加 轻微/一点/很 表示程度（如 轻微低落）；标注只描述本段的语速与情感基调",
            "- 标注行不是正文，不受逐字保留约束；但正文中除标注行外禁止出现任何方括号/花括号标签",
            "",
        ]
    else:
        lines += [
            "5. 输出必须是纯正文：不得添加任何【第N段】类标注行、方括号或花括号标签、Markdown 符号。",
            "",
        ]
    if experimental_tags:
        lines.append("6. 本次允许实验：最多使用 3 个方括号标签（如 [停顿两秒]、[轻声]、[强调]）来测试 TTS 对标签的响应；其余规则不变。")
    lines += ["", "【润色示范】", "原文：短视频里唐朝不存在的说法，不是一件猎奇异闻，而是一种认知毒瘤。"]
    if with_emotion:
        lines += ["润色：", "【第1段】语速：慢(1.35) | 情感：平静+坚定", "短视频里，唐朝不存在的说法——不是一件猎奇异闻，而是一种认知毒瘤。", "（标注行描述整段基调；正文字词与原文完全一致：只在断句处加逗号、在强调处用破折号，没有改写句子）"]
    else:
        lines += ["润色：短视频里，唐朝不存在的说法——不是一件猎奇异闻，而是一种认知毒瘤。", "（字词与原文完全一致：只在断句处加逗号、在强调处用破折号，没有改写句子）"]
    lines += ["", "【原文】", text]
    return "\n".join(lines)


def llm_optimize(text, style="casual", experimental_tags=False, with_emotion=False, hint="", relax=1.0):
    messages = [
        {"role": "system", "content": "你是中文口播稿标点编辑：只插入标点（含空行）控制停顿，逐字保留原文，不加任何词，绝不改写。"},
        {"role": "user", "content": build_prompt(text, style, experimental_tags, with_emotion, relax)},
    ]
    if hint:
        messages.append({"role": "user", "content": hint})
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.15,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    resp = requests.post(
        BASE_URL + "/chat/completions",
        headers={"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=600,
    )
    resp.raise_for_status()
    data = resp.json()
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
    return (content or "").strip() or None


def humanize(text, style="casual", experimental_tags=False, with_emotion=False):
    start = time.time()
    original = (text or "").strip()
    if not original:
        return {"error": "empty input"}
    clean = clean_rules(original)
    tts_hits = 0
    if _HUMANIZE_CFG.get("tts_normalize", True):
        tts_hits = tts_normalize_hits(clean)
        clean = tts_normalize_numbers(clean)  # 全流程唯一改字步骤，必须先于 fidelity 基线确定
    clean = normalize_section_titles(clean, API_KEY)
    baseline = clean  # fidelity 基线（含数字归一与小标题改写）
    cap = _filler_cap()
    tail_forbidden = bool(_HUMANIZE_CFG.get("tail_forbidden", True))
    stats = {
        "source": "rules",
        "originalLen": len(original),
        "optimizedLen": 0,
        "pauses": 0,
        "fillers": 0,
        "costMs": 0,
        "ratio": 1.0,
        "directives": 0,
        "ttsNormalized": tts_hits,
    }
    optimized = None
    fallback = None  # 忠实度过关、但其他项未过的候选（全轮失败时优先于纯规则兜底）
    llm_error = None
    passed = False
    if API_KEY:
        try:
            hint = ""
            for _ in range(3):
                optimized = llm_optimize(clean, style, experimental_tags, with_emotion, hint=hint)
                if not optimized:
                    hint = "上次没有输出。请直接给出润色结果：逐字保留原文，只插入标点（含空行）控制停顿，不要加任何词。"
                    continue
                dirs, body = extract_directives(optimized)
                # 尾缀优先：句尾语气词实测突兀，单独给精准反馈（若没删干净，下一轮会落在忠实度上）
                if tail_forbidden:
                    tails = _tail_violations(body, baseline)
                    if tails:
                        stats["tailFail"] = (stats.get("tailFail") or 0) + 1
                        hint = "上次输出在句尾加了语气词【" + "、".join(tails) + "】，这些词听感突兀、一律禁止（" + "、".join(_TAIL_WORDS) + "）。请只删掉这些句尾语气词，其余内容保持不动。"
                        continue
                # 忠实度：改写了原文的版本一律丢弃，不允许进入任何候选
                if not _fidelity_check(body, baseline):
                    stats["fidelityFail"] = (stats.get("fidelityFail") or 0) + 1
                    hint = "上次输出改写了原文，被拒绝。这次必须逐字保留原文，只插入标点（含空行），不能加字、改字、删字。标注行除外。"
                    continue
                fallback = optimized  # 忠实度过关即可留作保底候选
                over = _filler_overflow(body, baseline, cap)
                if over:
                    stats["fillerOverflowFail"] = (stats.get("fillerOverflowFail") or 0) + 1
                    detail = "；".join(f"「{w}」新增 {n} 次" for w, n in over.items())
                    hint = f"上次输出填充词超频（同一填充词全篇最多 {cap} 次）：{detail}。请删掉多余实例，原文和标点保持不动。"
                    continue
                ratio = len(body) / max(1, len(clean))
                red_flag = any(f in body for f in ["以下是", "修改后", "润色后", "改写后", "原文", "内容如下", "希望能帮到", "希望对你有帮助"])
                limit_max = {"soulful": 2.5, "energetic": 2.2, "casual": 2.0}.get(style, 2.0)
                # 流程只做加法，输出不应短于清洗稿；也不允许扩写超出上限
                if not (1.0 <= ratio <= limit_max) or red_flag:
                    stats["ratioFail"] = (stats.get("ratioFail") or 0) + 1
                    suffix = "，且开头有交代性文字（如 以下是/润色后）" if red_flag else ""
                    hint = f"上次输出篇幅是原文的 {ratio:.2f} 倍，超出允许区间 1.0~{limit_max}{suffix}。只插入标点（含空行），逐字保留原文，不要扩写、不要解释、不要加任何交代性文字。"
                    continue
                if _polish_delta(body, baseline) < 2:
                    stats["polishFail"] = (stats.get("polishFail") or 0) + 1
                    hint = "上次输出润色不足。这次至少插入 2 处停顿标点（逗号/省略号等），逐字保留原文。"
                    continue
                if with_emotion and _validate_directives(dirs) < 1:
                    stats["directivesFail"] = (stats.get("directivesFail") or 0) + 1
                    hint = "上次输出缺少段级标注行。必须把全文分成 2~6 段，每段开头独占一行输出标注：【第N段】语速：慢(1.35) | 情感：平静+轻微低落，语速词与数字、情感词从给定词表选择；正文逐字保留原文。"
                    continue
                passed = True
                stats["source"] = "llm"
                stats["ratio"] = round(ratio, 2)
                stats["directives"] = len(dirs)
                break
        except Exception as exc:
            optimized = None
            llm_error = str(exc)[:300]
    if not passed:
        optimized = fallback or rule_hesitate(clean)
    if _BREATH_CFG.get("enabled"):
        # 收口前的气口分布：和收口后的 stats["breath"] 配对，日志里直接能看出有没有收敛
        stats["breathBefore"] = breath_stats(optimized, over=int(_BREATH_CFG.get("max_span", 16)))
    optimized = postprocess(optimized, keep_tags=experimental_tags, with_emotion=with_emotion, baseline=baseline, tail_forbidden=tail_forbidden)
    if _HUMANIZE_CFG.get("tts_normalize", True):
        # LLM 改写可能把年份还原成阿拉伯数字（或漏转新形态），确定性后处理再兜一遍；
        # 幂等安全，且发生在 fidelity 检查之后，不影响基线判定
        re_hits = tts_normalize_hits(optimized)
        if re_hits:
            optimized = tts_normalize_numbers(optimized)
            stats["ttsRenormalized"] = re_hits
    stats["optimizedLen"] = len(optimized)
    stats["pauses"] = optimized.count("……")
    stats["fillers"] = len(_FILLER_RE.findall(optimized))
    if _BREATH_CFG.get("enabled"):
        # 气口分布（最长气口是否收敛）；stuck = 仍超阈值、没能找到安全断点的区间样本
        stats["breath"] = breath_stats(optimized, over=int(_BREATH_CFG.get("max_span", 16)))
    stats["costMs"] = int((time.time() - start) * 1000)
    over_final = _filler_overflow(optimized, baseline, cap)
    if over_final:
        stats["fillerOverflow"] = over_final  # 只记警告：硬删会误伤原文用词，交给重试解决
    residual = tts_residual_hits(optimized)
    if residual:
        stats["ttsResidual"] = residual  # 只记警告：TTS 可能读错的漏网数字
    if llm_error:
        stats["llmError"] = llm_error
    return {"text": optimized, "stats": stats, "style": style}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="口播稿拟人化优化")
    parser.add_argument("--style", default=DEFAULT_STYLE, choices=list(STYLES.keys()))
    parser.add_argument("--tags", action="store_true", help="允许方括号实验标签")
    parser.add_argument("--with-emotion", action="store_true", help="分段标注语速/情感（供支持标注的引擎消费）")
    args = parser.parse_args()

    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raw = sys.stdin.read()
    text, style, tags, with_emotion = "", args.style, args.tags, args.with_emotion
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("script") or "")
            style = str(payload.get("style") or args.style)
            tags = bool(payload.get("experimentalTags", args.tags))
            with_emotion = bool(payload.get("withEmotion", args.with_emotion))
        elif isinstance(payload, str):
            text = payload
    except Exception:
        text = raw
    result = humanize(text, style, tags, with_emotion)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
