# Decision Log — Overnight Audit 2026-05-07

Every non-trivial decision made by the PM Agent during this audit. One line each.

| Time | Decision | Rationale |
|---|---|---|
| Start | Working folder = `audit_2026-05-07/` subfolder | Keep audit artifacts isolated from main strategy docs; CLAUDE.md uses dated subfolders for time-bounded efforts |
| Start | Agent A scans BOTH `C:\Users\laxmi\Documents\Project` AND `C:\ContentOps\_pipeline\` + `C:\ContentOps\channels\ShadowVerse\` | Per CLAUDE.md, the actual production output lives at ContentOps; strategy lives at Documents\Project. A workflow audit must see both. |
| Start | Audit honors all "durable rules" in CLAUDE.md (sacred gates, no agent frameworks, git identity, audience pivot, LLM-API policy, YouTube upload approval gate) | Operator explicitly listed them as non-negotiable; relitigating them would burn the audit's credibility |
| Start | Agents told to prefer tavily-mcp / WebSearch / WebFetch in that priority order | Memory `feedback_tavily_mcp.md` says tavily-mcp is operator's preferred search MCP |
| Start | Audit dated 2026-05-07 even though it spans overnight | Operator's timezone start date; aligns with audience-pivot snapshot |
| W1→W2 | Wave 2 Agent G (AI visuals) instructed to **drop Sora 2** from the head-to-head | Sora 2 deprecated 2026-03-24, API shutdown 2026-09-24 (Agent C). Including it would waste analysis. Replacement field: Runway Gen-4 / Kling 3.0 / Veo 3.1 / Pika 2.x. |
| W1→W2 | Wave 2 Agent H (editing) constrained to **augment FFmpeg+Python pipeline**, not propose Premiere/Resolve/CapCut as the editor | Agent A confirmed pipeline is FFmpeg+Python. Operator built it deliberately. Recommending editor migration would relitigate scope. AI-first editors only viable as supplements (e.g. Submagic for captions). |
| W1→W2 | Wave 2 Agent I (audio/captions/export) instructed to specifically **fix the LUFS pipeline gap** (config says -14, output is -15.1 to -15.6, no loudnorm runs) and **propose per-platform LUFS variants** (-14 YT vs -10 to -12 TikTok/IG) | Both gaps identified by Agents A + C. Highest-leverage low-effort fix in the audit. |
| W1→W2 | Wave 2 Agent F (voiceover) instructed to model TTS cost at **3-5 Shorts/week + 1-2/day TikTok with ≥3-hr spacing**, NOT 1/day or 5/day | Agent D's cadence finding. Cost models in prior `.research/agent_*` reports used 1/day; out of date. |
| W3→W4 | All 7 Wave-3 conflicts resolved in `RESOLUTIONS.md`; phase docs left intact as appendix | Surgical editing of 9 phase docs = busy work. Single canonical RESOLUTIONS.md is cleaner audit trail. Master doc + Wave 4 + Agent N consume RESOLUTIONS.md. |
| R1 | Loudness uniform -14 LUFS / -1 dBTP across YT + TT + IG | -11 LUFS for TT/IG sourced only from OpusClip blog; APU Software 2026-05 contradicts (-16 LUFS); no platform-official target exists. Default to YT-verified -14 LUFS uniform. Re-test in 30 days. |
| R2 | ElevenLabs **Starter $5** as production default; Creator $22 = upgrade trigger at 1k subs OR YPP active OR ≥80 shorts/mo for 30 days OR A/B-proven >5% AVD lift from v3 audio tags | Channel is 3 days old, sub-100 subs, no revenue. $17 gap = real money at $0 income. Per `feedback_llm_api_policy.md`, Creator hasn't been demonstrated faster yet. Aligns Agents C/G/H. |
| R3 | Gameplay 0% — Agent G's recommendation wins | Daily Dot brain-rot fatigue + format mismatch + channel already ships 0% (description not pivot). |
| R3 | NOT autonomously editing CLAUDE.md project description; flagged in MORNING_BRIEF.md instead | Operating principle: "Ask first: anything that touches durable rules." CLAUDE.md is project constitution. |
| R4 | TikTok = brand/funnel surface, not direct-revenue surface | 30-50s format does not qualify for Creator Rewards (≥60s + 10K followers + 100K views/30d). Cost modeling stays $22→$5 ElevenLabs flat-fee, no incremental TT revenue assumed. |
| R5 | In-house Python (`caption_word_pop.py`) wins over Submagic | faster-whisper free + cached, ASS karaoke is ~150-250 LoC, Submagic auto-emoji is style-guide-banned, SaaS breaks automation. Agents H + I converged independently. |
| R6 | AI-disclosure narrow scope: only AI-of-real-people / altered-real-events / photoreal-synthetic | Verified YouTube policy text. Blanket disclosure penalizes ShadowVerse vs competitors who don't disclose. |
| R7 | Mono → stereo via `-ac 2` FFmpeg flag (dual-mono upmix) | Match platform spec. Trivial code change. |
| R8 | ElevenLabs ToS — perpetual license applies only to cloned-voice training data, NOT generated audio | Agent F's wording more precise than Agent C's. |
| R9 | Rename Agent B §1.1 header in master doc to "First-3-second swipe gate (post-Mar-2025 algorithm change)" | Avoids reading as generic creator advice when surfaced in master doc. |
| R10 | Bernard Films cited as "≈90K subs, ≈66-70M views, ≈19 videos late-2025/early-2026" | Directional band; numerical drift between Agent E (70M/93K) and Reddit thread (66M/90K) within ±10%. |
| R11 | rSlash profile flagged "synthesis pattern only, not recommended format" | Reddit-text + gameplay split is not ShadowVerse format. |
