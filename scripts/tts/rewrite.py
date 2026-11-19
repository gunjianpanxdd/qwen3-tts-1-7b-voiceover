# -*- coding: utf-8 -*-
"""
L1 结构编辑：把口播稿改成指定平台形态（B站长视频 / 抖音短视频）。

与 L2（humanize.py，只插标点、逐字保真）分工明确：
- 本文件允许改字、删字、重排（压缩、钩子前置、插入自我介绍）；
- humanize 拿本文件的输出当输入，铁律不变，两层规则不重叠。

契约：stdin 读 JSON，stdout 写 JSON（与 humanize.py 一致）。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import humanize as H  # noqa: E402  复用 .env 加载与 LLM 配置

API_KEY = os.environ.get("REWRITE_API_KEY") or H.API_KEY
BASE_URL = (os.environ.get("REWRITE_BASE_URL") or H.BASE_URL).rstrip("/")
MODEL = os.environ.get("REWRITE_MODEL") or H.MODEL
MAX_TOKENS = int(os.environ.get("REWRITE_MAX_TOKENS", H.MAX_TOKENS))

# 中文口播实测 5.5~6.5 字/秒，取中位 5.8；只用于算字数预算，不承诺精确时长
CHARS_PER_SEC = 5.8

PLATFORMS = {
    # 只保留「结构编辑」一种形态：钩子前置 + 插入自我介绍，不压缩。
    # 抖音压缩版（30/60/90 秒）于 2026-10-30 下线：抖音本身也有长视频模块，
    # 而 2500 字压到 500 字只能讲一个论点，主观感受是"核心信息全丢"，不如不做。
    # compress 机制保留（下面几处分支仍读它），日后若要重启短版，加一个 compress=True 的平台即可。
    "struct": {
        "name": "结构编辑版",
        "compress": False,
        "hook_max": 40,
        "sentence_max": 60,
        "delta": (0.85, 1.20),
        "brief": "面向长视频的口播稿：完整保留原稿全部论点与例子，允许长句（单句不超过 60 字）；"
                 "只做三件事——把最核心的那个问题提到第一句当钩子、按需插入自我介绍、必要时调整段落顺序让开头更紧凑。",
    },
}

# 开头范式（只有一种，2026-10-30 按真实爆款样本重做）。
# 样本：samples/01-为什么有人因为网暴而死而我感觉我能扛得住网暴.md
#   「为什么有人因为网暴而死，而我感觉我能扛得住网暴」
# 它不是"抛问题"，而是把听众自己的一个判断摆上台面，再让这个判断和事实打架。
# 这样听众会紧张（我的判断可能错了），而不是"嗯，确实"。
HOOK_SPEC = (
    "把听众自己的一个判断摆到台面上，再让它和事实形成反差。"
    "范本：「为什么有人因为网暴而死，而我感觉自己能扛得住？」——"
    "「我能扛」是听众的自信，这句话直接指向它，听众会想证明自己没错、于是听下去。"
)

# 口述体的开头是另一套逻辑：不抛悬念，先给一个听众能秒认的普遍判断，再回到自己。
# 依据 samples/口述类型.md：原文「课间我下楼买了瓶东方树叶…」→ 口播稿先加
# 「人越长大，越能分清身边两种过来人。一种嘴上教你做人，一种转头把事都给你办了」。
ORAL_HOOK_SPEC = (
    "口述体的开头不是抛悬念，而是先给一个听众能秒认的普遍判断，再回到自己的经历。"
    "范本：「不知道你有没有发现，人越长大，越能分清身边两种过来人。"
    "一种是嘴上教你做人，转头自顾自快活；一种是话不多说，转头把事都给你办了。」"
    "紧接着用一句「上学那会儿有件事我记到现在」把话头交回自己。"
)
# 口述体的三项硬处理（依据 samples/口播稿处理模板.md 第 3/4/5 步）
ORAL_PROCESS_LINES = [
    "## 口述体的三项硬处理（必须做）",
    "1. 引号全部拆掉：改成「他说，…」「我说，…」。听众看不到引号，不知道谁在说会瞬间出戏。",
    "2. 分段编号 01/02 删掉，换成时间型（那会儿/后来/再后来）或判断型（最绝的是这个）转场，不超过 8 个字。",
    "3. 阿拉伯数字转汉字：9 → 九，19 → 十九，2024 → 二〇二四。量词不能省（「一小时九块」比「时薪 9 元」好听）。",
    "4. 不要加「哎、啊、哈、就是说」凑口语感——那是假装自然，听众能听出来。",
    "",
]

# 口述体判据：第一人称密度高 + 有对话 + 有时间线
ORAL_TIME_WORDS = ("那会儿", "那年", "后来", "当时", "小时候", "上学", "有一次", "那天", "前几天", "去年")
# 只认明确的"分类框架"，不能收「一种」——观点文里「是一种很具体的疲惫」会被误判成口述
ORAL_GENRE_PATTERNS = ("两种", "一类", "一种是", "有没有发现", "不知道你有没有", "你会发现")
# 论述词密度高 → 观点文（口述体是事件流，不靠这些连接词推进）
ESSAY_MARKERS = ("本质", "其实", "因为", "所以", "这意味着", "换句话说", "问题在于", "第一", "第二")


def detect_genre(source: str) -> str:
    """粗分文体：oral（口述/第一人称叙事）或 essay（观点/干货）。

    口述体和观点文的开头逻辑相反（前者先给判断再讲故事，后者抛矛盾不给答案），
    用同一套范式必然有一边不对味。
    """
    head = strip_meta_lines(source or "")[:1500]
    if not head:
        return "essay"
    my = head.count("我")
    quotes = head.count("“") + head.count("\"")
    score = 0
    if my >= 5:
        score += 1
    if my >= 10:
        score += 1  # 第一人称密度特别高，基本就是口述
    if quotes >= 2:
        score += 1
    if any(w in head for w in ORAL_TIME_WORDS):
        score += 1
    # 反向：论证连接词密集说明是观点文（实测 2849 字观点稿因「是一种很具体的疲惫」被判成口述）
    if sum(head.count(w) for w in ESSAY_MARKERS) >= 5:
        score -= 1
    # 第二人称主导 = 观点文，不是第一人称口述（实测「你」17 次 /「我」9 次的观点稿被判成口述，
    # 于是套用「回到我」范式，把原文的「你」全改成「我」）
    you = head.count("你")
    if you >= 8 and you >= my * 1.5:
        score -= 2
    # 门槛放到 3：观点文实测能拿 2 分（7 个"我"+3 处引号+时间词-1），口述样本能拿 4 分
    return "oral" if score >= 3 else "essay"
# 开头是否已自带判断/情绪提炼（命中即说明不是"只有场景"，不该再补句子）
JUDGMENT_WORDS = ("不是", "说不上", "不全是", "算不上", "谈不上", "其实", "为什么",
                  "本质是", "问题在于", "不全然")
# 反差结构判据：有对立面（我 vs 有人 / 以为 vs 其实 / A 而 B）才算抓住了矛盾。
# 刻意不含「不是」——它太宽，像「那种感觉不是愤怒，也不是无奈」这种情绪描写会被误判成有对立面。
REVERSAL_WORDS = ("而", "却", "但", "其实", "原来", "反过来", "以为", "照样", "同样", "有人", "别人")
# 必须指向听众：抽象口号里没有"你/我"，只有概念名词
AUDIENCE_RE = re.compile(r"[你我咱]|大家|每个人|很多人|有人")
# 场景锚点：只用于 assess_opening 判断"这一段是不是只有场景"，
# 不作为校验项——真实好钩子「为什么有人因网暴而死，而我感觉我能扛」没有任何时间数字。
ANCHOR_RE = re.compile(
    r"\d|[零一二三四五六七八九十百千万]{1,3}[点岁年天次块个条句]|凌晨|半夜|清晨|傍晚|深夜|小时候|那年|去年|今天|昨天"
)
# 早期给答案的信号：出现在开头 300 字内，说明"结论提前"，只需要后移而不是重写。
# 「所以」必须带祈使/指向词才算（「所以别急着换面霜」✓，「所以哭那人就纳闷了」✗——那是故事里的转折）。
EARLY_ANSWER_RE = re.compile(r"其实是|原因是|缺的是|问题在|说到底|本质是|所以(?:别|不要|先|你|说)")
OPENING_VERDICTS = ("keep", "sharpen", "rewrite")
# 身份位置的中文说法（写进 prompt，让模型知道往哪放）
INTRO_SLOT_LABELS = {
    "auto": "自动，跟着第一个完整段落之后",
    "pitch": "承诺之后、正文之前",
    "tail": "全片结尾、正文之后",
    "both": "承诺之后 + 全片结尾各一次",
    "hook_embed": "并进第一句，不单独成段",
    "after-hook": "承诺之后、正文之前（旧值，等同 pitch）",
}

# 权威型身份：身份本身就是信息，可以并进钩子（唯一允许"开头带身份"的情况）
AUTHORITY_WORDS = ("律师", "医生", "医师", "会计", "工程师", "教师", "老师", "记者", "研究员",
                   "分析师", "咨询师", "营养师", "设计师", "教练", "从业", "执业", "年经验")


def decide_intro_slot(intro):
    """按身份性质决定自我介绍放哪。资料共识（2026-11-03 检索）：

    - 主流做法是**开头 3 秒不放自我介绍**：推荐流观众不是你的粉丝，前 3 秒要留给内容
      （律师起号复盘：用「大家好我是 X 律师」开头，完播率低于 20%）。
    - 身份放结尾是近因效应（papi酱「我是 papi酱，一个集美貌与才华于一身的女子」）。
    - 唯一例外是"身份即信息"的权威背书（律师/医生/从业十年），但这时身份要**并进钩子**：
      「作为从业十年的律师，这 3 条你别签」——不是单独一段「我是 X 律师」。
    """
    ident = (intro or {}).get("identity") or {}
    who = "%s %s" % (ident.get("who") or "", ident.get("topic") or "")
    if any(word in who for word in AUTHORITY_WORDS):
        return "hook_embed"
    return "tail"

HOOK_BANNED = ["大家好", "今天我们来", "最近很多人问", "今天想跟大家", "哈喽", "hello", "我是新人"]
STRUCT_TAGS = ("[HOOK]", "[/HOOK]", "[PITCH]", "[/PITCH]", "[INTRO]", "[/INTRO]", "[INTRO2]", "[/INTRO2]",
               "[BODY]", "[/BODY]", "[OUTRO]", "[/OUTRO]", "[CORE]", "[/CORE]")
SENT_SPLIT = re.compile(r"(?<=[。！？!?])")
# 结构标记成对性：少一个闭合标记，strip 后会把标记本身当正文念出来
BLOCK_PAIRS = (
    ("[HOOK]", "[/HOOK]"),
    ("[PITCH]", "[/PITCH]"),
    ("[INTRO]", "[/INTRO]"),
    ("[INTRO2]", "[/INTRO2]"),
    ("[BODY]", "[/BODY]"),
)
INTRO_BLOCK_RE = re.compile(r"\[INTRO\]([\s\S]*?)\[/INTRO\]")
# 核心问题：全文只回答这一个问题，钩子必须直指它
CORE_BLOCK_RE = re.compile(r"\[CORE\]([\s\S]*?)\[/CORE\]")
# 判「同一句话」时去掉全部标点与空白：模型复述时常换标点（。→ ，）
PUNCT_RE = re.compile(r"[\s，。！？、,.!?…—\-—\"'“”‘’（）()【】\[\]：:；;]")


def char_budget(platform: str, target_sec=None, source_len=None):
    """返回 (lo, hi) 汉字字数区间。

    当前只有 struct（不压缩）：按原稿长度换算，允许因插介绍变长。
    取整用 round：int() 截断会把 1000*1.15=1149.9999 砍成 1149，凭浮点误差少给 1 字。
    """
    preset = PLATFORMS.get(platform) or PLATFORMS["struct"]
    if preset["compress"]:
        budget = float(target_sec or 60) * CHARS_PER_SEC
    else:
        budget = float(source_len or 0)
    lo_d, hi_d = preset["delta"]
    return int(round(budget * lo_d)), int(round(budget * hi_d))


def plan_for(platform: str, target_sec=None, source_len=None, hook="auto", intro=None, opening="keep"):
    return {
        "platform": platform if platform in PLATFORMS else "struct",
        "targetSec": target_sec,
        # 只剩一种开头范式，hook 入参保留只为兼容老调用方
        "hook": "auto",
        "intro": intro or {"enabled": False},
        # 开头处置：默认 keep（不乱改原稿开头），rewrite() 里会按 assess_opening 覆盖
        "opening": opening if opening in OPENING_VERDICTS else "keep",
        "openingReason": "",
        # 原稿字数：决定"下界是不是硬要求"。原稿本来就短时不能逼模型扩写。
        "sourceLen": int(source_len or 0),
        "budget": char_budget(platform, target_sec, source_len),
    }


def strip_marks(text: str) -> str:
    """去掉结构标记，只留正文（标记是校验用的，绝不能进最终稿件）。

    保留段落空行：\n\n 是 TTS 分段留白（paragraph_gap_ms）的唯一依据。
    旧实现 `if line.strip()` 把所有空行删掉，导致 L1 落稿多段并成一段
    （2026-11-06 samples/比较长篇.md 里"正文段落被合并"的真正原因）。
    """
    for tag in STRUCT_TAGS:
        text = text.replace(tag, "")
    out, blank = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            out.append(stripped)
            blank = False
        elif out and not blank:
            out.append("")  # 连续空行压成一个，保留段落边界
            blank = True
    return "\n".join(out).strip()


def han_len(text: str) -> int:
    """只数汉字/字母/数字：与口播时长直接相关的是这个数，标点空白不算。"""
    return len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text or ""))


def first_sentence(text: str) -> str:
    body = strip_marks(text or "").strip()
    parts = [p for p in SENT_SPLIT.split(body) if p.strip()]
    return parts[0].strip() if parts else ""


def check_hook(sentence: str, platform="struct"):
    preset = PLATFORMS.get(platform) or PLATFORMS["struct"]
    problems = []
    plain = re.sub(r"\s", "", sentence or "")
    if not plain:
        return ["第一句为空：钩子句必须有内容"]
    if han_len(plain) > preset["hook_max"]:
        problems.append(
            "第一句超长：%d 字，上限 %d 字（钩子要短，一句说完）" % (han_len(plain), preset["hook_max"])
        )
    for bad in HOOK_BANNED:
        if bad and bad in plain:
            problems.append(
                "第一句命中了黑名单开场「%s」：这类寒暄是抖音前 3 秒流失主因，必须换成结论/问题/反差" % bad
            )
    return problems


# 「不是A，是B」式定义句：在开头出现通常等于把答案先说了（命名式结论）
DEFINITION_RE = re.compile(r"不是.{0,10}?是")
# 揭示词：钩子里出现"其实是/原来是"，等于当场把答案揭了。
# 范本的钩子只摆矛盾（为什么有人致死，而我感觉能扛），答案留到正文。
REVEAL_WORDS = ("其实是", "其实就", "原来是", "本质上", "说白了", "真相是", "无非是", "归根到底")


def check_hook_shape(sentence: str, platform="struct", genre="essay"):
    """钩子体检：有对立面 + 指向听众。

    早期版本要求"具体锚点（时间/数字/动作）"，但真实好开头「为什么有人因为网暴而死，
    而我感觉自己能扛得住」里既没有时间也没有数字——那条规则会把好钩子判死，已废弃。
    """
    plain = re.sub(r"\s", "", sentence or "")
    if not plain:
        return []
    problems = []
    has_reversal = any(w in plain for w in REVERSAL_WORDS)
    # 口述体的"对立面"是"两种人/一种…另一种"，不靠转折词
    if genre == "oral" and any(w in plain for w in ORAL_GENRE_PATTERNS):
        has_reversal = True
    if not has_reversal:
        problems.append(
            "钩子没有对立面：把一个反差点摆出来（我 vs 别人、以为 vs 其实、A 而 B），"
            "比如「为什么别人扛不住，而我感觉自己能扛？」"
        )
    if not AUDIENCE_RE.search(plain):
        problems.append("钩子没指向听众：让它说的是「你/我」的处境，而不是抽象概念（代际差异/认知升级这类）")
    if DEFINITION_RE.search(plain) and "？" not in plain and "?" not in plain:
        problems.append(
            "钩子在开头就给出了命名式结论（「不是…是…」）：这通常是全文的答案，"
            "换成把矛盾摆出来（听众的判断 vs 事实），答案留到结尾"
        )
    if any(w in plain for w in REVEAL_WORDS):
        problems.append(
            "钩子把答案揭了（出现「其实是/原来是」这类词）：范本只说『为什么有人致死，而我感觉能扛』，"
            "不解释原因。钩子只摆矛盾，答案放到正文第二句之后"
        )
    return problems


def resolve_intro_slot(intro):
    """确定最终位置：用户指定优先，否则按身份性质判断；未启用时按结尾处理。"""
    intro = intro or {}
    if not intro.get("enabled"):
        return "tail"
    return intro.get("slot") or decide_intro_slot(intro)


def strip_meta_lines(text: str) -> str:
    """去掉稿子头部的 markdown 标题与引用元数据（`# 第143期…`、`> 采集时间…`）。

    样本文件普遍带这些行，不去掉的话"开头 200 字"里全是标题和元数据，
    正文一个字没读到，判定必然失真（实测 143 期因此被误判成"只有场景"）。
    """
    lines = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith(">"):
            continue
        if set(stripped) <= set("-*_ ") and stripped:  # --- / *** / ___ 分隔线
            continue
        lines.append(stripped)
    return "\n".join(lines)


def assess_opening(source: str, genre: str = "essay"):
    """判断原稿开头要不要动、动多少。纯规则，不调 LLM。

    设计原则：**默认 keep**。只有判定为寒暄/纯背景才重写；答案提前、只有场景都只做最小改动。
    早期版本对所有文案强制重写开头，把本来成立的开头（网暴、115 期、143 期）也改了，属于过度加工。
    """
    text = strip_meta_lines((source or "").strip())
    if not text:
        return {"verdict": "keep", "reason": "空稿"}

    # 口述体的判据完全相反：直接开讲事件是常态，但要补一句可认同的判断（依据 samples/口述类型.md）
    if genre == "oral":
        head = text[:220]
        if any(w in head for w in ORAL_GENRE_PATTERNS):
            return {"verdict": "keep", "reason": "开头已经有判断，不需要改"}
        return {"verdict": "rewrite", "reason": "口述体直接开讲事件：先补一句能认同的判断，再回到自己"}
    head = text[:400]
    slice200 = text[:200]
    plain_head = PUNCT_RE.sub("", head)
    first = first_sentence(text)

    # 寒暄只看开头 60 字：转写稿常常整篇没有句末标点，first_sentence 会把
    # 自我介绍处的「大家好」也吞进来，导致整篇被误判成寒暄开场。
    head60 = text[:60]
    banned = next((w for w in HOOK_BANNED if w in head60), "")
    if banned:
        return {"verdict": "rewrite", "reason": "寒暄开场「%s」：直接进冲突" % banned}

    # 冲突/人称看开头 200 字，而不是只看第一句：
    # 143 期开头是「人到底该不该信命。先别急着回答。我给你讲一个…」，
    # 只看第一句会把它误判成"纯背景"而重写。
    has_reversal = any(w in slice200 for w in REVERSAL_WORDS)
    has_you = bool(AUDIENCE_RE.search(slice200))
    has_anchor = bool(ANCHOR_RE.search(slice200))

    if not has_reversal and not has_you and not has_anchor:
        return {"verdict": "rewrite", "reason": "开头是纯背景/通报，既没有冲突也没有具体场景"}

    early_match = EARLY_ANSWER_RE.search(plain_head)
    if early_match:
        return {"verdict": "sharpen",
                "reason": "答案提前（开头 300 字内出现「%s」）：把结论后移即可" % early_match.group(0)}

    # 有承诺信号说明开头已经成型（143 期「我给你讲一个两千年前的故事」就是这种），
    # 不能被"有场景"误判成需要修
    has_pitch = any(w in slice200 for w in PITCH_WORDS)
    # 开头已经自带判断/情绪提炼时不算"只有场景"。
    # 注意要认「不全是/说不上/算不上」这类否定式判断——只匹配「不是」会漏
    # （实测「说不上是愤怒，也不全是无奈」漏判，导致仍然去补句子）。
    has_judgment = any(w in text[:400] for w in JUDGMENT_WORDS)
    if has_anchor and not has_reversal and not has_pitch and not has_judgment:
        # 只有场景时也**不补句子**：补出来的内容必然是原文没有的，
        # 且会插在段间打断上下文、常用「其实」剧透（2026-11-10 两次实测）
        return {"verdict": "keep", "reason": "开头只有场景，但没有可依据的判断：保持原样，不补句子"}
    return {"verdict": "keep", "reason": "开头已经成立，不需要改"}


def count_intro(text: str, intro_text) -> int:
    # intro_text 可能是 None 或 identity 字典（AI 模式）：非字符串一律当"没有可比对的原文"。
    # 早期版本直接把它喂给 re.sub，dict 触发 TypeError，被 main() 兜底吞成 bad payload。
    if not isinstance(intro_text, str):
        return 0
    key = re.sub(r"\s", "", intro_text)[:12]
    if not key:
        return 0
    return re.sub(r"\s", "", text or "").count(key)


def check_hook_repeat(body: str):
    """钩子被提到第一句后，正文里的原句必须删掉——否则全篇同一句说两遍。"""
    first = first_sentence(body)
    key = PUNCT_RE.sub("", first)
    if len(key) < 8:
        return []
    rest = body.replace(first, "", 1)
    if key[:12] in PUNCT_RE.sub("", rest):
        return ["第一句（钩子）在正文里又重复了一遍：钩子只保留一次，正文里那一遍要删掉"]
    return []


def dedupe_opening(text: str):
    """删掉开头被写了两遍的第一句，返回 (文本, 是否修过)。

    `[HOOK]` 与 `[BODY]` 边界不清时，模型会把第一句写进 HOOK、又在 BODY 里
    从原稿开头重新抄一遍（2026-11-06 实测：「凌晨三点手机还亮着…」连续出现两次）。
    这类重复是完全一样的整句，纯规则删掉即可，不必再烧一次 LLM；
    而且旧校验只在 rewrite 模式跑，keep/sharpen 会整只漏过去。
    """
    s = text or ""
    m = re.match(r"\s*([^。！？!?…\n]{8,120}[。！？!?…])", s)
    if not m:
        return s, False
    first = m.group(1)
    prefix = s[: m.end()]
    rest = s[m.end():]
    stripped_rest = rest.lstrip()
    if not stripped_rest.startswith(first):
        return s, False
    cut = len(rest) - len(stripped_rest) + len(first)
    return prefix.rstrip() + " " + rest[cut:].lstrip(), True


# 句首/段首的孤立逗号：「…普通人翻身的故事。 ，然后…」「…十五秒一块。 ，所以…」
# 成因：在连词「然后/所以/因为」之前插逗号时，连词正好落在新句开头。
_ORPHAN_COMMA_RE = re.compile(r"(?<=[。！？!?…\s])，(?=[^\s，。！？])")


def clean_orphan_commas(text: str) -> str:
    """删掉前面只有空白/句末标点的逗号（正常中文逗号前不会有空格）。"""
    return _ORPHAN_COMMA_RE.sub("", text or "")


def dedupe_adjacent_blocks(text: str):
    """删掉任意位置「相邻 k 句被整段又抄一遍」的重复，返回 (文本, 是否修过)。

    不能只查末尾：身份单独成段放在最后时，重复段并不贴着结尾（实测身份行
    「我是闲云冶翯…」挡住了后缀匹配）。忽略标点空白差异，只认 ≥30 字的重复，
    正常的短金句回扣不受影响。
    """
    s = text or ""
    parts = [p for p in re.split(r"(?<=[。！？!?…])", s) if p.strip()]
    if len(parts) < 2:
        return s, False

    def norm(x: str) -> str:
        return PUNCT_RE.sub("", re.sub(r"\s", "", x))

    for k in range(min(12, len(parts) // 2), 0, -1):
        for i in range(len(parts) - 2 * k + 1):
            a = "".join(parts[i: i + k])
            b = "".join(parts[i + k: i + 2 * k])
            if len(norm(b)) >= 30 and norm(a) == norm(b):
                kept = parts[: i + k] + parts[i + 2 * k:]
                return "".join(kept).rstrip(), True
    return text, False


def sanitize_candidate(text: str):
    """落稿前的确定性清理：重复开头/结尾段 + 句首孤立逗号。

    这些都是模型的机械性失误，纯规则能修，不必再烧一轮 LLM。
    """
    text, dedup_head = dedupe_opening(text)
    text, dedup_tail = dedupe_adjacent_blocks(text)
    text = clean_orphan_commas(text)
    return text, {"dedupHead": dedup_head, "dedupTail": dedup_tail}


def split_core(raw: str):
    """把 [CORE]...[/CORE] 摘出来，返回 (去掉该段的文本, 核心问题)。"""
    text = raw or ""
    found = CORE_BLOCK_RE.search(text)
    core = re.sub(r"\s+", "", found.group(1)) if found else ""
    return CORE_BLOCK_RE.sub("", text), core


def check_hook_source(hook_sentence: str, plan: dict):
    """钩子不能原样搬原稿结尾的句子——那是结论/金句，放开头等于剧透。"""
    key = PUNCT_RE.sub("", hook_sentence or "")[:12]
    if len(key) < 8:
        return []
    tail = plan.get("sourceTail") or ""
    if tail and key in tail:
        return [
            "钩子直接搬了原稿结尾的句子：那是结论，放在开头等于剧透，听众不会想知道后面。"
            "换成一句具体场景或反差事实（时间+动作），结论留到正文结尾。"
        ]
    return []


# 承诺信号：路径（我给你讲/今天看/举个例子）或价值（我保证/你会发现）
PITCH_WORDS = ("给你讲", "带你", "今天看", "举个例子", "一个故事", "三个", "我保证", "你会发现",
               "看完", "听完", "记载", "出自", "接下来讲", "拆成", "讲清", "算给你", "看这三", "分两层")
# 身份块里不许出现的时间副词过渡（「大家好」不在此列——真实样本都在用）
INTRO_BAD_TRANSITIONS = ("说到这儿", "说到这", "接下来我们", "顺便说一句", "话说回来", "另外说", "那么我们")


def block_body(text: str, open_tag: str, close_tag: str) -> str:
    m = re.search(re.escape(open_tag) + r"([\s\S]*?)" + re.escape(close_tag), text or "")
    return m.group(1).strip() if m else ""


def check_blocks(raw: str, plan: dict):
    """剥离标记之前的结构体检：标记成对、承诺/身份块按需存在、位置正确、原话逐字出现。

    AI 写身份时无法用文本匹配验证（每次措辞不同），只能查结构块在不在；
    话术库模式则可以逐字比对，所以这里按块比对而不是按全文。
    """
    problems = []
    text = raw or ""
    for open_tag, close_tag in BLOCK_PAIRS:
        if text.count(open_tag) != text.count(close_tag):
            problems.append("结构标记不成对：%s 与 %s 数量不一致，标记必须成对出现" % (open_tag, close_tag))

    intro = plan.get("intro") or {}
    enabled = bool(intro.get("enabled"))
    slot = intro.get("slot") or "auto"
    intro_body = block_body(text, "[INTRO]", "[/INTRO]")
    intro2_body = block_body(text, "[INTRO2]", "[/INTRO2]")
    pitch_body = block_body(text, "[PITCH]", "[/PITCH]")

    if enabled and slot != "hook_embed" and not intro_body:
        problems.append("缺少身份块：必须在 [INTRO]...[/INTRO] 之间写内容，且不能为空")
    if not enabled and (intro_body or intro2_body):
        problems.append("身份块未启用，但输出了 [INTRO]/[INTRO2]：整段删掉，一个字都不要留")
    if not enabled:
        # 模型可能绕过标记块，把自我介绍直接写进 [BODY]（实测「大家好，我是做代际观察的…」）
        plain = strip_marks(text)
        if re.search(r"大家好[，,]\s*我是|我是做[^，。]{0,12}的[，,]|我是[^，。]{0,12}[，,]\s*专讲", plain):
            problems.append("没有开启自我介绍，正文里却多了一段身份：整句删掉，不要编造任何身份信息")
    if enabled and slot == "both" and not intro2_body:
        problems.append("位置选了「承诺后+结尾」，必须再输出一段 [INTRO2]...[/INTRO2]")
    if enabled and slot != "both" and intro2_body:
        problems.append("只选了一处身份，却输出了 [INTRO2]：删掉多余这段")

    # 承诺块：只要身份不在结尾（tail），就必须先给听众一个"东西"再报名。
    # 早期只在 pitch/both 要求，auto 时只查块内承接 → 模型写出「这个场景你也许真的遇到过」
    # 这种零信息的废话承接，读起来像复述上文（2026-11-02 实测）。
    if enabled and slot in ("auto", "pitch", "both"):
        if not pitch_body:
            problems.append(
                "缺少承诺块 [PITCH]：报身份之前，先给听众一个东西——"
                "我准备讲什么（比如「我把这件事拆成两层」）或你会得到什么"
            )
        else:
            if not any(w in pitch_body for w in PITCH_WORDS):
                problems.append(
                    "承诺块不像承诺（缺「给你讲/今天看/举个例子/我保证/你会发现」这类信号）："
                    "写成路径承诺（我用什么回答你）或价值承诺（你会得到什么），不要复述上文"
                )
            # 承诺复述钩子 = 换句话把标题再说一遍，等于没有承诺
            hook_key = PUNCT_RE.sub("", first_sentence(strip_marks(text)))
            pitch_key = PUNCT_RE.sub("", pitch_body)
            if len(hook_key) >= 8 and hook_key[:12] in pitch_key:
                problems.append("承诺块在复述钩子：这里要往前推一步（给承诺/给路径），不是把钩子再说一遍")

    # 位置：身份不能跟在钩子后面裸奔
    if enabled and intro_body:
        hook_end = text.find("[/HOOK]")
        intro_start = text.find("[INTRO]")
        body_end = text.find("[/BODY]")
        if hook_end >= 0 and intro_start >= 0 and intro_start <= hook_end:
            problems.append("身份块跑到钩子前面了：钩子必须先完整出现")
        if slot in ("pitch", "both"):
            pitch_end = text.find("[/PITCH]")
            if pitch_end >= 0 and intro_start >= 0 and intro_start < pitch_end:
                problems.append("位置是「承诺之后」，身份必须排在 [PITCH] 之后")
        if slot == "tail" and body_end >= 0 and intro_start >= 0 and intro_start < body_end:
            problems.append("位置是「全片结尾」，身份必须排在正文 [BODY] 之后")

    # 身份必须是"承接 + 身份"，不能裸奔报名。
    # 输出格式里 [INTRO] 固定在 [BODY] 之前，块外没有承接的空间，
    # 所以承接句必须写在块内第一句——早期只在块外查"钩子与身份之间有没有内容"，
    # 与格式自相矛盾，模型两轮都改不掉，最终裸奔放行（2026-11-02 实测）。
    ident = intro.get("identity") or {}
    ident_fields = [str(ident.get(k) or "").strip() for k in ("who", "topic", "series") if str(ident.get(k) or "").strip()]

    # 权威型：身份并进钩子第一句，不单独成段
    if enabled and slot == "hook_embed":
        if intro_body:
            problems.append("身份要并进钩子第一句，不要单独输出 [INTRO] 段")
        who = str(ident.get("who") or "").strip()
        topic = str(ident.get("topic") or "").strip()
        hook_text = first_sentence(strip_marks(text))
        # 允许三种写法：带名字（张律师）、带领域（合同纠纷）、带职业（律师）
        hit = bool((who and who in hook_text) or (topic and topic in hook_text))
        if not hit:
            role = next((w for w in AUTHORITY_WORDS if w in who or w in topic), "")
            hit = bool(role and role in hook_text)
        if not hit:
            problems.append(
                "权威型身份必须写进钩子第一句（带上「%s」或「%s」）：身份本身就是信息，"
                "比如「作为从业十年的律师，这份合同里这3条你别签」" % (who or "你的身份", topic or "你的领域")
            )

    if enabled and intro_body and ident_fields:
        who = str(ident.get("who") or "").strip()
        if who and who not in intro_body:
            problems.append(
                "身份里的名字必须原样出现（你给的是「%s」）：不能改字、不能换成近义词，否则账号名会变样" % who
            )
        # 身份块里不要写"这个场景/这种事"这类指代复述——它是承接语治表象的产物，纯废话
        if re.match(r"^[「『\"“]?(这个|这种|这样|这点|刚才|上面|前面|刚才说的)", intro_body):
            problems.append(
                "身份块开头在复述上文（「这个/这种/刚才」）：这里只说你是谁、讲什么，"
                "承接交给前面的 [PITCH] 承诺块，不要在身份里重复场景"
            )

    for word in INTRO_BAD_TRANSITIONS:
        if word in intro_body:
            problems.append("身份块里不要用「%s」这种时间副词过渡：改用承接内容的承诺句" % word)

    # 话术库原话：必须在各自块内逐字出现 1 次
    intro_text = intro.get("text")
    if enabled and isinstance(intro_text, str) and intro_text.strip():
        for label, body in (("[INTRO]", intro_body), ("[INTRO2]", intro2_body)):
            if not body:
                continue
            if count_intro(body, intro_text) != 1:
                problems.append("%s 里的原话必须逐字出现 1 次（不要改写、不要只出现一部分）：%s" % (label, intro_text))
    return problems


def validate(text: str, plan: dict):
    """返回问题列表（空 = 通过）。每条都写成给 LLM 的具体指令，可直接当重试 hint。"""
    problems = []
    if not (text or "").strip():
        return ["输出为空：必须输出完整改写稿"]
    for tag in STRUCT_TAGS:
        if tag in text:
            problems.append("残留了结构标记 %s：输出只能有正文，标记必须全部去掉" % tag)

    body = strip_marks(text)
    preset = PLATFORMS.get(plan.get("platform")) or PLATFORMS["struct"]

    # 开头校验按处置分支：keep 模式一个都不查（原稿开头本来就成立，
    # 再拿"必须有对立面/必须有锚点"去卡它，等于把好开头判死）。
    platform = plan.get("platform", "struct")
    opening = plan.get("opening") or "keep"
    genre = plan.get("genre") or "essay"
    # 首句重复与"开头动不动"无关：keep 模式同样会犯（HOOK 写一遍、BODY 又抄一遍），
    # 所以无条件查（旧版只在 rewrite 模式查，keep/sharpen 整只漏过去）。
    problems.extend(check_hook_repeat(body))
    if opening == "rewrite":
        hook_problems = check_hook(first_sentence(body), platform)
        hook_problems.extend(check_hook_source(first_sentence(body), plan))
        hook_problems.extend(check_hook_shape(first_sentence(body), platform, genre))
        problems.extend(hook_problems)
        # 口述体：先给判断，第二句必须回到「我」，否则听众不知道这是你的亲身经历
        if genre == "oral":
            first_two = "".join([s for s in SENT_SPLIT.split(body) if s.strip()][:2])
            if "我" not in first_two:
                problems.append(
                    "口述体开头要先给判断、紧接一句回到「我」："
                    "比如「上学那会儿有件事我记到现在」，让听众知道是你亲身经历的"
                )

    # 口述体的三项硬处理（模板第 3/4/5 步）：不管开头动不动，都要查
    if genre == "oral":
        if re.search(r"[“”\"]", body):
            problems.append("还有引号：全部拆成「他说，…」「我说，…」，听众看不到引号，不知道谁在说")
        if re.search(r"(?m)^\s*\d{2}\s*$", body):
            problems.append("还有 01/02 这种分段编号：换成时间型（后来/再后来）或判断型（最绝的是这个）转场，不超过 8 个字")
        if re.search(r"\d", body):
            problems.append("还有阿拉伯数字：转成汉字（9→九，2024→二〇二四），量词不能省")
    elif opening == "sharpen":
        # 开头保留原稿，只拦「把结尾金句搬上来当第一句」
        problems.extend(check_hook_source(first_sentence(body), plan))

    # 不压缩的形态只防"跑偏"：扩写或大砍都不是结构编辑该做的事。
    n = han_len(body)
    source_len = int(plan.get("sourceLen") or 0)
    if source_len >= 100:
        if n > int(source_len * 1.30):
            problems.append(
                "篇幅超出原稿 30%%（原稿 %d 字，现在 %d 字）：结构编辑只重排开头和插介绍，不要扩写内容"
                % (source_len, n)
            )
        elif n < int(source_len * 0.70):
            problems.append(
                "篇幅只剩原稿约 %d%%（原稿 %d 字，现在 %d 字）：不要把论点砍掉，只重排开头"
                % (int(round(n * 100.0 / max(1, source_len))), source_len, n)
            )

    # 段落不能被合并：段落留白是 TTS 拼接层给的（paragraph_gap_ms），段没了留白就没了
    src = plan.get("source") or ""
    if src:
        src_paras = len([p for p in re.split(r"\n\s*\n", src) if p.strip()])
        out_paras = len([p for p in re.split(r"\n\s*\n", body) if p.strip()])
        if src_paras >= 3 and out_paras < max(2, int(src_paras * 0.6)):
            problems.append(
                "原稿的自然段被合并了（%d 段 → %d 段）：保留空行分段，"
                "不要把多个自然段并进一段，段落之间要给 TTS 留白" % (src_paras, out_paras)
            )
        # 正文保真：除"删重复段/换首句"外不该有别的改动。改字会连带影响 TTS 读音与韵律
        # （2026-11-10 实测：同音替换 3 个字，听感和回译都下降）。
        if len(src) >= 200:
            a = PUNCT_RE.sub("", re.sub(r"\s", "", src))
            b = PUNCT_RE.sub("", re.sub(r"\s", "", body))
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            if ratio < 0.90:
                problems.append(
                    "正文改动过多（与原稿字面相似度 %.0f%%）：只允许删掉与新开头重复的那一段，"
                    "其余字句照抄——改字会让 TTS 的读音和韵律变差" % (ratio * 100)
                )
        # 观点文里"我"暴增 = 把原文的「你」改成了「我」（口述范式误用的典型后果）
        if genre == "essay":
            src_my, out_my = src[:2000].count("我"), body[:2000].count("我")
            src_you, out_you = src[:2000].count("你"), body[:2000].count("你")
            # 组合判据：「我」变多 + 「你」变少 = 第二人称被改写（单纯倍数会漏掉小改动）
            if out_my - src_my >= 3 and src_you - out_you >= 3:
                problems.append(
                    "人称被改了（原稿「你」%d 处→%d 处，「我」%d 处→%d 处）："
                    "原稿用「你」就保持「你」，不要为了满足开头范式把第二人称改写成第一人称"
                    % (src_you, out_you, src_my, out_my)
                )

    # 身份块与「话术库原话逐字出现」的校验已全部移到 check_blocks：
    # 那里拿得到带标记的 raw，能按 [INTRO] / [INTRO2] 分块比对（both 时两块各一次）。
    # 这里绝不能把 identity 字典当文本传下去（曾整条链路崩成 bad payload）。

    if preset["compress"]:
        sents = [s for s in SENT_SPLIT.split(body) if s.strip()]
        long_sents = [s for s in sents if han_len(s) > preset["sentence_max"]]
        if sents and len(long_sents) > max(1, int(len(sents) * 0.2)):
            problems.append(
                "有 %d 句超过 %d 字：短视频要一句一口气，在语义自然处断开" % (len(long_sents), preset["sentence_max"])
            )
    return problems


def build_prompt(text: str, plan: dict) -> str:
    preset = PLATFORMS[plan["platform"]]
    source_len = int(plan.get("sourceLen") or 0)
    # 不压缩：篇幅保持原稿规模即可（插入自我介绍允许略长）
    length_line = "篇幅保持原稿规模（约 %d 字）：插自我介绍可以略长，但不要扩写原文内容，也不要砍掉论点。" % source_len
    intro = plan.get("intro") or {}
    intro_block = "不要写自我介绍，一个字都不要加。"
    if intro.get("enabled"):
        slot_now = intro.get("slot") or "tail"
        slot_desc = {
            "hook_embed": "第一句里（和钩子并成一句）",
            "auto": "第一个完整段落之后",
            "pitch": "承诺之后",
            "tail": "全稿最末、收尾之后单独一段",
            "both": "开头附近 + 全稿最末各一次",
            "after-hook": "承诺之后",
        }.get(slot_now, "全稿最末、收尾之后单独一段")
        body = intro.get("text") or ""
        if body:
            intro_block = (
                "把下面这段身份信息放到【%s】，逐字照抄、一个字都不改：\n「%s」\n"
                "不要复述、不要加解释。" % (slot_desc, body)
            )
        else:
            ident = intro.get("identity") or {}
            # 只拼已填的字段：早期版本给未填项填了「（未填）」占位符，模型会照抄并念出来
            wanted = []
            if (ident.get("who") or "").strip():
                wanted.append("我是谁「%s」" % ident["who"].strip())
            if (ident.get("topic") or "").strip():
                wanted.append("讲什么「%s」" % ident["topic"].strip())
            if (ident.get("series") or "").strip():
                wanted.append("当前系列「%s」" % ident["series"].strip())
            intro_block = (
                "在【%s】写一句自我介绍（不超过 45 字），包含：%s。"
                "不要写承接语、不要复述上文，名字必须原样用我给的写法。"
                % (slot_desc, "、".join(wanted))
            )
    opening = plan.get("opening") or "keep"
    opening_reason = plan.get("openingReason") or ""
    intro = plan.get("intro") or {}
    intro_slot = intro.get("slot") or "auto"
    # 除结尾位置外，报身份前都要先给承诺（tail 时前面是整篇正文，不需要）
    need_pitch = bool(intro.get("enabled")) and intro_slot in ("auto", "pitch", "both")

    lines = [
        "你是中文口播稿的结构编辑。任务：把原稿改成%s。" % preset["name"],
        "",
        "## 这个版本要做什么",
        preset["brief"],
        length_line,
        "",
        "## 第一步：先定核心问题（写进 [CORE]）",
        "写出听众心里的一个具体误判：他以为 A，其实是 B。不超过 30 字。",
        "范本：「我以为自己扛得住网暴，其实我根本不知道网暴是什么」。",
        "",
    ]

    # 开头怎么处理取决于对原稿的判定——不是所有文案都要重写开头。
    if opening == "keep":
        lines.extend([
            "## 开头：保持原样（本次不改）",
            "判定：%s。" % (opening_reason or "原稿开头已经成立"),
            "第一句、第一段一律照抄：不要重写、不要润色、不要调换语序。",
            "唯一允许的改动：如果原开头与后文有重复，删掉重复的那一半。",
            "",
        ])
    elif opening == "sharpen":
        lines.extend([
            "## 开头：只挪动，不新增",
            "判定：%s。" % (opening_reason or "开头只需微调"),
            "原稿开头的场景、画面、细节全部保留，第一句照抄。只做一件事：",
            "把开头 300 字内那句结论/机制句剪下来，挪到正文中后段，开头留悬念。",
            "**不许新增任何句子**：不要为了制造反差自己写一句（实测会打断上下文、还会用「其实」剧透）。",
            "",
        ])
    elif (plan.get("genre") or "essay") == "oral":
        # 口述体：先给判断再回到自己（依据 samples/口述类型.md）
        lines.extend([
            "## 开头：重写（口述体：先给判断，再回到自己）",
            "判定：%s。" % (opening_reason or "原稿开头不成立"),
            ORAL_HOOK_SPEC,
            "第一句不得超过 %d 字，禁止这些开场：%s。" % (preset["hook_max"], "、".join(HOOK_BANNED)),
            "第二句必须回到「我」，用一个具体时间点（那会儿/那年/有一次）把话头交给自己。",
            "",
            "钩子正误对照（口述体）：",
            "✓ 「人越长大，越能分清身边两种过来人。一种嘴上教你做人，一种转头把事给你办了」——先给可认同的判断",
            "✓ 「上学那会儿有件事我记到现在」——紧跟一句把话头交回自己",
            "✗ 「课间我下楼买了瓶东方树叶，回教室被两位老大哥拦住了」——直接开讲事件，听众不知道你要说什么",
            "✗ 「大家好，今天给大家讲个故事」——寒暄式开场",
            "",
        ])
        lines.extend(ORAL_PROCESS_LINES)
    else:
        lines.extend([
            "## 开头：重写（原稿开头是寒暄或纯背景）",
            "判定：%s。" % (opening_reason or "原稿开头不成立"),
            HOOK_SPEC,
            "第一句不得超过 %d 字，禁止这些开场：%s。" % (preset["hook_max"], "、".join(HOOK_BANNED)),
            "钩子必须直指 [CORE]，并且有对立面（我 vs 别人 / 我以为 vs 事实 / A 而 B）。",
            "钩子只摆矛盾，不许解释：不要出现「其实是/原来是/说白了」这类揭示词。",
            "",
            "钩子正误对照：",
            "✓ 「为什么有人因为网暴而死，而我感觉自己能扛得住」——把听众的自信摆上台面，和事实打架，但不解释原因",
            "✓ 「我妈转发养生文，配了张她二十岁的黑白照。她当年信的东西，现在还算数吗？」——我 vs 她、当年 vs 现在",
            "✗ 「你以为自己的累是矫情，其实是几代人的折叠」——把答案揭在开头了，只留矛盾即可",
            "✗ 「凌晨两点，你盯着家族群一条转发，手指停在屏幕上」——只有画面，没有对立面",
            "✗ 「今天我们聊聊代际差异」——寒暄式开场",
            "",
        ])

    if need_pitch:
        lines.extend([
            "## 承诺：先给听众一个东西（写进 [PITCH]）",
            "报身份之前必须先给承诺：听众要知道留下来能拿到什么，才愿意听你是谁。",
            "三种写法任选：",
            "· 路径承诺：我把这件事拆成两层，你想象的那层，和真正压垮人的那层",
            "· 事实承诺：今天看三个案例，出自《宋史》《新五代史》《禁书》",
            "· 价值承诺：听完你会有种把人生方向盘重新拿回来的感觉",
            "不超过 40 字。不要复述钩子（那是把标题再说一遍），不要用「说到这儿」「接下来」这类时间副词。",
            "",
        ])

    if not intro.get("enabled"):
        # 早期这里无条件输出「加身份」整段指令 + 范本，只有 intro_block 写着"不要写"，
        # 规则自相矛盾 → 模型没勾选也编了一句自我介绍（2026-11-10 实测）。
        lines.extend([
            "## 身份：本次不加入",
            "正文里不得出现「大家好，我是…」「我是…，专讲…」这类自我介绍句；",
            "也不要输出 [INTRO]/[INTRO2] 块。一个字都不要加。",
            "",
        ])
    elif intro_slot == "hook_embed":
        lines.extend([
            "## 身份：并进第一句，不要单独成段",
            "身份是权威背书，直接写进钩子第一句，和钩子合成一句话：",
            "✓ 「作为从业十年的律师，这份合同里这3条你别签」",
            "✗ 「大家好，我是X律师。这份合同里这3条你别签。」——身份单独占一句，浪费前 3 秒",
            "名字必须用我给的原字，一个字都不能改；不要输出 [INTRO] 段。",
            intro_block,
            "",
        ])
    elif intro_slot == "tail":
        lines.extend([
            "## 加身份（写进 [INTRO]，放在**全片最末**）",
            "**开头一个字都不要插**：身份单独成段放在收尾之后，不要去拆原稿的开头。",
            "身份块只写两件事：我是谁 + 我讲什么。",
            "范本：「我是每天一点历史学，专讲历史里那些被忽略的细节。」",
            "名字必须用我给的原字，一个字都不能改。",
            intro_block,
            "",
        ])
    else:
        lines.extend([
            "## 加身份（写进 [INTRO]，位置：%s）" % INTRO_SLOT_LABELS.get(intro_slot, intro_slot),
            "身份块只写两件事：我是谁 + 我讲什么。",
            "不要写承接语（「这个场景你也许遇到过」这种是废话），也不要在身份里重复上文。",
            "范本：「大家好，我是每天一点历史学，专讲历史里那些被忽略的细节。」",
            "「大家好」可以用（真实样本都这么开场）；名字必须用我给的原字，一个字都不能改。",
            intro_block,
            "",
        ])

    lines.extend([
        "",
        "## 正文（写进 [BODY]）—— 一个字都不许改",
        "原稿的字句、语序、人称一律照抄：不换词、不改人称、不合并或拆分句子、不调整语序。",
        "允许的改动只有一种：删掉与新开头重复的那一小段（同一内容只保留一处）。",
        "不要添任何新句子（包括「能被复述的机制句」——原稿有就保留，没有就不要造）。",
        "**原稿的空行分段必须保留**：不要把多个自然段并进同一段（段落之间要给 TTS 留白）。",
        "",
        "## 输出格式（严格遵守，标记必须成对出现）",
        "[CORE]这篇要回答的核心问题（一句话）[/CORE]",
        "[HOOK]第一句[/HOOK]",
        "（注意：[BODY] 从第二句开始，不要把 [HOOK] 那句再抄一遍，也不要把原稿开头重复一次）",
    ])
    if need_pitch:
        lines.append("[PITCH]承诺：我准备用什么回答你（不超过 40 字）[/PITCH]")
    if intro.get("enabled") and intro_slot in ("auto", "pitch", "both"):
        lines.append("[INTRO]身份（未启用则整段不输出）[/INTRO]")
    lines.extend([
        "[BODY]正文[/BODY]",
        "[OUTRO]结尾（原稿没有可省略）[/OUTRO]",
    ])
    if intro.get("enabled") and intro_slot == "tail":
        lines.append("[INTRO]身份（放在全片最末，收尾之后单独一段）[/INTRO]")
    if intro.get("enabled") and intro_slot == "both":
        lines.append("[INTRO2]结尾处再出现一次身份（可与开头略作变化，但原话仍是同一句）[/INTRO2]")
    lines.extend([
        "",
        "## 铁律",
        "1. 原稿的事实、数字、人名、结论、人称（你/我/他）一个字都不许编造或改动。",
        "2. 只输出上述标记块，不要任何解释、前言、结语。",
        "3. 标记块之后不要重复正文。",
        "4. 正文除「删掉重复段」外不得有任何字词改动：实测改一个字就会让 TTS 的读音和韵律变差。",
        "",
        "## 原稿",
        text,
    ])
    return "\n".join(lines)


def call_llm(prompt: str, temperature=0.6) -> str:
    import requests

    payload = {
        "model": MODEL,
        "temperature": temperature,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": "你是中文口播稿结构编辑，只输出带结构标记的正文，不输出任何解释。"},
            {"role": "user", "content": prompt},
        ],
    }
    resp = requests.post(
        BASE_URL + "/chat/completions",
        headers={"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=600,
    )
    resp.raise_for_status()
    content = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", content.strip()).strip()


def rewrite(text: str, opts: dict) -> dict:
    """软失败：两轮都不过也返回最佳候选 + failed 原因，绝不返回空稿。"""
    start = time.time()
    source = (text or "").strip()
    if not source:
        return {"error": "empty input"}
    plan = plan_for(
        opts.get("platform") or "struct",
        opts.get("targetSec"),
        han_len(source),
        opts.get("hook") or "auto",
        opts.get("intro"),
    )
    # 原稿结尾 300 字（去标点）：用来拦「把结尾金句搬到开头当钩子」这种剧透式开场
    plan["sourceTail"] = PUNCT_RE.sub("", source)[-300:]
    # 文体：口述体与观点文的开头逻辑相反，必须分开写
    plan["genre"] = detect_genre(source)
    # 原稿留作段落数校验的基线（LLM 常把多个自然段并成一段）
    plan["source"] = source
    # 开头判定：用户选了具体处置就用它，否则自动判（默认 keep，不乱改）
    opening_mode = (opts.get("opening") or "auto").strip()
    if opening_mode in ("keep", "sharpen", "rewrite"):
        plan["opening"] = opening_mode
        plan["openingReason"] = "用户指定为「%s」" % opening_mode
    else:
        verdict = assess_opening(source, plan["genre"])
        plan["opening"] = verdict["verdict"]
        plan["openingReason"] = verdict["reason"]
    intro = plan.get("intro") or {}
    if intro.get("enabled"):
        # 未指定位置时按身份性质判断：权威型并进钩子，其余放结尾
        intro["slot"] = resolve_intro_slot(intro)
    plan["intro"] = intro
    plan["introSlot"] = intro.get("slot") or "tail"

    stats = {
        "platform": plan["platform"],
        "sourceLen": han_len(source),
        "targetSec": plan["targetSec"],
        "budget": list(plan["budget"]),
        "hook": plan["hook"],
        "intro": bool(intro.get("enabled")),
        "introSlot": plan["introSlot"],
        # 开头判定必须回传：用户要能看到"这篇开头到底改没改、为什么"
        "opening": plan["opening"],
        "openingReason": plan["openingReason"],
        "genre": plan.get("genre") or "essay",
        "rounds": 0,
        "failed": [],
        "outLen": 0,
        "costMs": 0,
    }
    if not API_KEY:
        stats["failed"] = ["missing api key：未配置 REWRITE_/HUMANIZE_/OPENAI_ API key"]
        return {"text": source, "stats": stats, "platform": plan["platform"], "rewritten": False}

    best, best_problems, best_core = None, None, ""
    hint = ""
    for _ in range(2):
        stats["rounds"] += 1
        prompt = build_prompt(source, plan)
        if hint:
            prompt += "\n\n上次的问题：\n" + hint
        try:
            raw = call_llm(prompt)
        except Exception as err:  # 网络/超时：保留原稿，用户可手改，不阻塞
            stats["failed"] = ["llm error: %s" % err]
            stats["costMs"] = int((time.time() - start) * 1000)
            return {"text": source, "stats": stats, "platform": plan["platform"], "rewritten": False}
        # 先摘掉 [CORE]（只给用户看核心问题），再体检结构（看带标记的 raw），
        # 最后跑正文规则（看剥好的落稿）。
        body_raw, core = split_core(raw)
        candidate = strip_marks(body_raw)
        candidate, fixes = sanitize_candidate(candidate)
        if any(fixes.values()):
            stats["dedupFix"] = [k for k, v in fixes.items() if v]
        problems = check_blocks(body_raw, plan) + validate(candidate, plan)
        if best is None or len(problems) < len(best_problems):
            best, best_problems, best_core = candidate, problems, core
        if not problems:
            break
        hint = "\n".join("- " + p for p in problems)

    stats["failed"] = best_problems or []
    # 核心问题：钩子指向的那一个问题，用户一眼就能判断"这是不是我要的重点"
    stats["core"] = best_core
    stats["outLen"] = han_len(best or "")
    stats["costMs"] = int((time.time() - start) * 1000)
    return {"text": best or source, "stats": stats, "platform": plan["platform"], "rewritten": not best_problems}


def main():
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {"text": raw}
        result = rewrite(payload.get("text") or payload.get("script") or "", payload)
    except Exception as err:
        # 绝不能把 raw 当正文返回：raw 是请求的 payload JSON，下游会把它当稿子处理
        # （实测会产出带 {"text":" 外壳的"优化稿"，并让 humanize 三轮忠实度全败）。
        result = {"text": "", "stats": {"failed": ["bad payload: %s" % err]}, "rewritten": False}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
