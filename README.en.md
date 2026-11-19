# qwen3-tts-1-7b-voiceover

> Voiceover script optimization specifically for Qwen3-TTS 1.7B Base — preserve every character, adjust only punctuation and structure.

Turns written drafts into voiceover-ready scripts for Qwen3-TTS 1.7B Base. Unlike "de-AI" approaches built for large models with human pick-best loops, this skill works within this model's measured limits: punctuation only, no character changes, and no tokens it mispronounces. Every rule comes from testing on this model — switching models or versions requires re-measurement.

## What it does

- **Text only** — no audio generation, no TTS calls, no changes to article arguments or structure.
- **Two layers** — L1 (`scripts/tts/rewrite.py`) handles structural editing (hook-first, genre routing, oral-style fixes); L2 (`scripts/tts/humanize.py`) handles punctuation humanization (character-exact, pause-only).
- **Self-healing checks** — fidelity / tail particles / weak polish / length overruns trigger automatic retries with hints.

## Core constraints (why it is designed this way)

Measured traits of Qwen3-TTS 1.7B Base shape the design; these are not style preferences:

| Model trait | Countermeasure |
|---|---|
| Particles are read as literal sounds | Never add new ones; keep those already in the source |
| Fillers (um / like / you know) are read literally | Never insert; use …… for pauses only |
| Em dashes have no stable pause | Replace with commas or periods |
| Segment-level speed/emotion tags are ignored | Tag logic disabled by default |
| Years are misread as cardinal numbers | Read digits one by one, before the fidelity baseline |

## Relationship to kaikou

kaikou targets large models with human refinement (render → listen → pick 4 takes → add particles). This skill is its inverse: no human-in-the-loop, most of kaikou's text rules already covered, fixed cost. See the "Conclusion comparison with kaikou" section in `SKILL.md`.

## Usage

In WorkBuddy the skill loads automatically when the task involves voiceover script preparation for Qwen3-TTS 1.7B Base, narration humanization, or diagnosing machine-like delivery. Read `SKILL.md` directly for the full rules.

## Files

- `SKILL.md` — full rules and workflow (main file)
- `scripts/tts/` — bundled scripts: L1 structural editing, L2 punctuation humanization, number normalization
- `voice/config.yaml` — humanize / breath configuration (text-layer parameters only)
- `README.md` — Chinese documentation
- `LICENSE` — MIT

## License

MIT, see `LICENSE`.
