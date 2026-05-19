# Prompt library (extracted from `03_Prompt_Library.md`)

Each `.md` file in this folder is a single prompt template.

## How the pipeline loads them

```python
from pipeline import load_prompt
prompt = load_prompt("03_script_generation", config)
prompt = prompt.replace("{NICHE}", "...").replace("{TOPIC}", "...")
```

Substitution is **plain `str.replace`**, not Python's `str.format`. Several prompts contain
`{e.g., "..."}` style examples whose contents would break `format()`. Using `.replace()`
keeps the prompts readable and copy-pasteable to a chat UI when you want to manually
debug one.

## Files

| File                              | Source §  | Used by stage                  |
|-----------------------------------|-----------|--------------------------------|
| 01_niche_style_guide_template.md  | §1        | (filled once per channel — see Channels/ShadowVerse/style_guide.md for the filled version) |
| 02_idea_generation.md             | §2        | Monday — operator runs after trend_pull |
| 03_script_generation.md           | §3        | `pipeline.generate_script()` |
| 04_hook_optimization.md           | §4        | Optional — operator-driven when more hook variants needed |
| 05_fact_check.md                  | §5        | `pipeline.fact_check_script()` |
| 06_metadata_generation.md         | §6        | `pipeline.generate_metadata()` |
| 07_niche_validation.md            | §7        | Operator-driven when considering a new topic cluster |
| 08_weekly_analytics_review.md     | §8        | Sunday — operator runs after analytics_pull |
| 09_repurposing_decision.md        | §9        | Operator-driven when re-cutting old content |
| 10_pre_publish_self_check.md      | §10       | `pipeline.fact_check_script()` follow-up OR final QA paranoia pass |

## Source of truth

The strategy doc `C:\Users\laxmi\Documents\Project\03_Prompt_Library.md` is the canonical
version. If you edit a prompt here, update the strategy doc too — they should not drift.
