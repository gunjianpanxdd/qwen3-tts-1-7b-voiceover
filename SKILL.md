---
name: qwen3-tts-1-7b-voiceover
description: Optimizes written scripts into Qwen3-TTS 1.7B Base voiceover-ready text — preserve every character, adjust only punctuation and structure, and avoid tokens this specific model mispronounces. This skill should be used when preparing a narration draft for Qwen3-TTS 1.7B Base, adjusting pause rhythm, choosing a humanization style, or diagnosing machine-like delivery caused by the script text. Trigger words: 口播稿优化 / 配音稿 / 播稿拟人化 / Qwen3-TTS 配音 / Qwen 口播 / 去机器腔 / 朗读稿准备. Text only — does not generate audio and does not fix article structure or argument quality.
agent_created: true
version: "1.0.0"
display_name: Qwen3-TTS 1.7B 口播稿优化
display_name_en: Qwen3-TTS 1.7B Voiceover Script Optimizer
description_zh: 把书面稿优化成专供 Qwen3-TTS 1.7B Base 朗读的口播稿：逐字保真、只调标点与结构，绕开该模型实测会念错的语气词、破折号与标注。只做文字稿，不生成音频。
description_en: Turns written drafts into voiceover-ready scripts specifically for Qwen3-TTS 1.7B Base — preserve every character, adjust only punctuation and structure, and avoid tokens this model mispronounces. Text only; no audio generation.
---

# Qwen3-TTS 1.7B 口播稿优化

## 定位与边界

- 专用于 Qwen3-TTS 1.7B Base 的口播文字稿优化方案，把书面稿转成该模型朗读不出戏的稿子。
- 只做文字稿：不生成音频、不调用 TTS、不负责文章正文的观点与结构质量（那是生成侧 writer 的职责）。
- 目标不是无限逼近真人说话，而是在 Qwen3-TTS 1.7B Base 的能力边界内，消除「会出戏的文字因素」。
- 所有判据都来自该模型实测（含双 seed 受控对照）；换模型或换版本不能直接套用，须重新实测后再改规则。

## 随包脚本（代码即规则，本文件不参与运行时判定）

以下脚本已随 skill 内置，路径相对本 skill 根目录；改判据先改代码、再同步本文件，两者不一致以代码为准。

| 内容 | 唯一事实源 |
|---|---|
| L1 结构编辑 | `scripts/tts/rewrite.py`（PLATFORMS / HOOK_SPEC / detect_genre / 口述体三项硬处理） |
| L2 标点拟人化 | `scripts/tts/humanize.py`（STYLES / _TAIL_WORDS / _FILLER_WORDS / clean_rules / fidelity 校验 / build_prompt） |
| 数字归一 | `scripts/tts/utils/text_splitter.py`（年份口语化，L1 与 L2 共用） |
| 配置项 | `voice/config.yaml`（humanize 段、breath 段） |

### 环境与调用

- 依赖 Python 3.10+ 与 `requests`。两个脚本只走 OpenAI 兼容的 `/chat/completions` 协议，**不绑定任何 LLM 厂商或模型**：自行设置 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`，本地模型、云端 API、任意兼容服务都可以（`OPENAI_BASE_URL` 填接口前缀，脚本会拼接 `/chat/completions`）。也可把这三个变量写进 skill 根目录的 `.env` 供脚本自动加载。L1 / L2 可分别用 `REWRITE_*` / `HUMANIZE_*` 前缀覆盖（如 `REWRITE_MODEL` 只影响 L1），两者互不干扰。
- 两个脚本均为 stdin 读 JSON、stdout 写 JSON：
  - L1：`python scripts/tts/rewrite.py < draft.json > l1.json`，输入 `{"text": "原稿"}`。
  - L2：`python scripts/tts/humanize.py --style casual < l1.json > final.json`，输入 `{"text": "..."}`。
- 未配置 key 时走规则兜底或保留原稿，不阻塞流程但没有优化效果。

## Qwen3-TTS 1.7B Base 硬约束（实测，所有设计的根因）

以下每条都是该模型的实测结论，不是通用朗读规律；不要照搬面向大模型的「去 AI 味」教程，也不要直接套给别的 TTS：

1. 语气词（吧/啊/呢/嘛）会念成实读音 → 严禁 LLM 新增；原文自带的必须原样保留（校验按相对基线的差集判定）。
2. 填充词（嗯/呃/唔、那个/就是说/其实/你知道吗）会念成实读音 → 一律不插；犹豫与停顿只用 ……。
3. 破折号 —— 读不出稳定停顿 → 一律改成逗号或句号。
4. 段级语速/情感标注行不被识别（with_emotion 默认关闭）→ 模型侧不消费；标注逻辑保留代码但默认关。
5. 数字归一（年份口语化）是 L2 唯一改字步骤，必须先于 fidelity 基线确定，两条路径行为一致。

推论：本层是「标点润色 + 逐字保真」，不是「改写润色」。

## 两层分工

### L1 结构编辑（rewrite.py）

- 允许改字、删字、重排；当前只保留 `struct`（长视频）一种形态：钩子前置 + 按需插自我介绍 + 必要时段落调序，不压缩。
- 钩子：把听众自己的一个判断摆上台面，再让它与事实形成反差；不是抛问题、不给标准答案。
- 文体分流：`detect_genre` 分 oral（第一人称叙事）/ essay（观点干货），两者开头范式不同。
- 口述体三项硬处理：引号拆成「他说…/我说…」、编号换时间或判断型转场（≤8 字）、阿拉伯数字转汉字；不加填充词凑口语。

### L2 标点拟人化（humanize.py）

- 铁律：只插标点（含空行）控停顿，逐字保真。`fidelity` 校验：输出去标点后必须与基线逐字一致，不一致一律丢弃。
- 三档风格：soulful（深夜电台）/ energetic（激情演讲）/ casual（朋友聊天）；参数只调省略号上限、篇幅倍率、句末标点偏好，不是改写强度。
- 气口（breath）：`max_span` 20 / `hard_max` 28，默认关；需要时从 `voice/config.yaml` 开。
- 校验失败自动重试：`tailFail`（句尾语气词）、`fidelityFail`（改写）、`polishFail`（润色不足）、篇幅超限、`directivesFail`——每类带 hint 重问 LLM。

## 标准工作流

1. 判定文体（oral / essay）。
2. 判断是否需要 L1：原稿开头是否已有钩子、是否口述体（需三项硬处理）；不需要就跳过。
3. 将 L1 输出作为 L2 输入。
4. L2 选风格档：默认 `casual`；氛围/情绪类用 `soulful`，鼓动类用 `energetic`。
5. 按「环境与调用」的命令依次跑 L1（若需要）/ L2，读 stdout 的 stats：`fidelityFail`/`tailFail` 为 0 且篇幅在 1.0~上限时接受；否则按 hint 重试（脚本已自动）。
6. 成稿听感仍有问题时：先分辨是文字侧（本 skill 范围）还是模型侧（音色/参考音频，不在本 skill 范围）。

## 红线

- 不新增语气词、填充词、起手词（你知道吗/我告诉你）。
- 不改字、不拆句、不合并、不调语序（L2）；破折号换成逗号。
- 不输出任何方括号/花括号标签、emoji、Markdown 符号。
- 不把「加语气词/口头禅/方言词」当优化目标——那是面向大模型 + 人工挑句的做法，Qwen3-TTS 1.7B Base 会念成实读音。

## 与 kaikou（开口）的结论性对比（不要重复分析）

kaikou 是面向高参数模型 + 人工精修的 skill，设计与本方案相反：它先配音再逐句听、一句配 4 遍人工挑句、主动加语气词/口头禅/方言。

- 它的流程不适合商业化：一句 4 遍 + 人工听 + 重配，无法无人值守跑批；本方案是一次 rewrite + 一次 humanize + 一次 TTS，成本固定。
- 它的文本规则大多已有：套话（在…的今天/随着…发展）、破折号、「不是…而是…」、句长变异都已在 humanize prompt 禁止；一口气长度 `breath` 已有（20/28，只是默认关）。
- 唯一真差异是「补全省略 + 单字改双字」（它实测字数 +12.1%），但与 fidelity 铁律冲突，属 writer 生成侧改造，需单独立项评估。
- 结论：本方案没有可直接从 kaikou 搬来的东西；若再有人提议搬它的规则，先按上面三条核对现状。

## 提优化建议的纪律

给出口播优化建议前，先逐条核对现有规则（rewrite.py / humanize.py / build_prompt / config.yaml），分三类处理：

1. 已经有的 → 不要当新建议提。
2. 无实证收益的 → 不要提。
3. 与其他铁律冲突的 → 标为立项，不要当补丁。

三笔账同时报：prompt 净字符、输出字数变化、规则条数变化；只报一笔会掩盖副作用。
