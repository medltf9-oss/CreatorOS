# Agent Catalog — 111 agents

_Generated from `creatoros/agents/catalog.py` (catalog version `2026-11-p4.1`). This file is documentation; the database holds the seed plus any human edits._

Every agent has a fixed id, department, output contract, allowed tools and a system prompt. Ids never change: renaming an agent changes `name`, not `agent_id`.

## Structure

| Layer | Count | Delegates? | Contract |
|---|---|---|---|
| Chief | 1 | yes, to leaders | `plan` |
| Leader | 10 | yes, to own specialists | `plan` |
| Specialist | 100 | no (leaf) | role dependent |

Maximum delegation depth is 3 (chief → leader → specialist), so nothing can create work below a specialist.

## The chief

**chief_orchestrator** — Chief Orchestrator. Receives the user's objective, plans it, delegates to department leaders and compiles the verified result.

Duties:

- Classify the intent and decide which departments must contribute
- Create one task per department and state what that department must return
- Pick the order and dependencies between department tasks
- Compile the final answer from department results and quality verdicts
- Report honestly when part of the objective could not be completed, and why

## Departments

| # | Department | Mission | Leader | Specialists |
|---|---|---|---|---|
| 10 | **Strategic Intelligence** (`strategic_intelligence`) | Decide what to make and why: niches, audiences, positioning, campaigns and growth direction. | `strategy_director` | 10 |
| 20 | **Research and Intelligence** (`research_intelligence`) | Collect, verify and synthesise evidence so strategy and copy rest on facts, not guesses. | `research_director` | 10 |
| 30 | **Writing and Creative Development** (`creative_writing`) | Turn briefs into ideas, scripts, hooks, captions, titles and calls to action. | `creative_director` | 10 |
| 40 | **Video Production** (`video_production`) | Inspect, cut, caption and format video with deterministic tools instead of guessing. | `video_production_director` | 10 |
| 50 | **Graphic Design and Visual Identity** (`design_visual`) | Define and apply the visual system: typography, colour, layout, thumbnails and consistency. | `design_director` | 10 |
| 60 | **Platform Adaptation** (`platform_adaptation`) | Adapt one piece of content per platform without pretending the rules are the same. | `platform_director` | 10 |
| 70 | **Publishing Operations** (`publishing_operations`) | Prepare, validate and record publishing plans; never publish without explicit human approval. | `publishing_director` | 10 |
| 80 | **Audience and Community** (`audience_community`) | Understand audience response and draft respectful replies; no automated mass engagement. | `community_director` | 10 |
| 90 | **Analytics and Growth** (`analytics_growth`) | Measure what actually happened and propose experiments grounded in stored metrics. | `analytics_director` | 10 |
| 100 | **Quality and Governance** (`quality_governance`) | Review, verify and reject work independently of the department that produced it. | `quality_director` | 10 |

### Strategic Intelligence — `strategic_intelligence`

**Leader: Strategy Director** (`strategy_director`, `planner` profile). Decide the direction: niche, audience, positioning and campaign shape, with reasons and trade-offs.

| Specialist | Id | Output | Profile | Mission |
|---|---|---|---|---|
| Content Strategy Agent | `content_strategy_agent` | `analysis` | planner | Turn a business objective into content pillars, formats and a sequencing logic. |
| Audience Strategy Agent | `audience_strategy_agent` | `analysis` | specialist | Describe the audience segments that matter and what each one actually wants to watch. |
| Niche Research Agent | `niche_research_agent` | `analysis` | specialist | Identify niches where this account can win, with evidence and a difficulty judgement. |
| Competitor Analysis Agent | `competitor_analysis_agent` | `analysis` | specialist | Describe what competing accounts do, what works for them and where the gap is. |
| Trend Strategy Agent | `trend_strategy_agent` | `analysis` | specialist | Decide which trends fit the brand and which are noise, with a shelf-life judgement. |
| Market Positioning Agent | `market_positioning_agent` | `analysis` | planner | Write a positioning statement and the proof points behind it. |
| Campaign Planning Agent | `campaign_planning_agent` | `plan` | planner | Turn a goal into a campaign with a hook sequence, post count and success condition. |
| Growth Strategy Agent | `growth_strategy_agent` | `analysis` | planner | Recommend the growth levers worth pulling next and what to ignore. |
| Brand Strategy Agent | `brand_strategy_agent` | `analysis` | specialist | Define the brand voice, its boundaries and the phrases it must never use. |
| Business Objectives Agent | `business_objectives_agent` | `analysis` | planner | Translate content work into the business outcome it serves and how it will be judged. |

### Research and Intelligence — `research_intelligence`

**Leader: Research Director** (`research_director`, `planner` profile). Produce research briefs that separate verified facts from assumptions and speculation.

| Specialist | Id | Output | Profile | Mission |
|---|---|---|---|---|
| Web Research Agent | `web_research_agent` | `analysis` | specialist | Collect relevant material and record exactly where each claim came from. |
| Source Verification Agent | `source_verification_agent` | `analysis` | router_light | Check each claim against its cited source and say plainly when it does not hold. |
| Fact Checking Agent | `fact_checking_agent` | `analysis` | router_light | Identify claims that need verification and state what evidence would settle them. |
| Topic Discovery Agent | `topic_discovery_agent` | `analysis` | specialist | Propose topics with a specific angle and a reason the audience would care. |
| Keyword Research Agent | `keyword_research_agent` | `analysis` | router_light | Propose keywords and phrases people would actually search, grouped by intent. |
| Hashtag Research Agent | `hashtag_research_agent` | `analysis` | router_light | Propose hashtag sets with a mix of reach levels, per platform. |
| Audience Question Agent | `audience_question_agent` | `analysis` | specialist | List the questions the audience actually asks and rank them by intent. |
| Trend Research Agent | `trend_research_agent` | `analysis` | specialist | Document trends with what is observable and what is inferred, kept separate. |
| Reference Collection Agent | `reference_collection_agent` | `report` | specialist | Assemble references a creator can actually use: examples, structures, visual notes. |
| Research Synthesis Agent | `research_synthesis_agent` | `report` | planner | Merge the departmental findings into one brief without losing the uncertainty. |

### Writing and Creative Development — `creative_writing`

**Leader: Creative Director** (`creative_director`, `specialist` profile). Make the copy sharp, on-brand and original, and reject filler.

| Specialist | Id | Output | Profile | Mission |
|---|---|---|---|---|
| Idea Generation Agent | `idea_generation_agent` | `analysis` | specialist | Generate distinct, filmable ideas with a reason each could work. |
| Storytelling Agent | `storytelling_agent` | `copy` | specialist | Shape a story with a clear tension, turn and payoff that fits the duration. |
| Script Writer Agent | `script_writer_agent` | `copy` | specialist | Write a script that sounds natural when read aloud, timed to the target duration. |
| Short Video Script Agent | `short_video_script_agent` | `copy` | specialist | Write a short-form script whose first line earns the next three seconds. |
| Educational Script Agent | `educational_script_agent` | `copy` | specialist | Explain one thing clearly without dumbing it down or padding it. |
| Entertainment Script Agent | `entertainment_script_agent` | `copy` | specialist | Write something genuinely entertaining without clickbait that the content cannot pay off. |
| Hook Writer Agent | `hook_writer_agent` | `copy` | specialist | Write hooks that are true to the content and stop the scroll. |
| Caption Writer Agent | `caption_writer_agent` | `copy` | specialist | Write captions that add something the video does not already say. |
| Title Writer Agent | `title_writer_agent` | `copy` | specialist | Write titles that are accurate, specific and searchable. |
| Call To Action Agent | `call_to_action_agent` | `copy` | specialist | Write a next step the viewer can actually take and would want to. |

### Video Production — `video_production`

**Leader: Video Production Director** (`video_production_director`, `specialist` profile). Get a correct, platform-ready video file using deterministic processing wherever possible.

| Specialist | Id | Output | Profile | Mission |
|---|---|---|---|---|
| Video Import Agent | `video_import_agent` | `analysis` | router_light | Check that an incoming file is usable and record its real properties. |
| Video Inspection Agent | `video_inspection_agent` | `analysis` | router_light | Report the technical facts of a video file and what they imply for publishing. |
| Scene Detection Agent | `scene_detection_agent` | `analysis` | router_light | Identify scene cuts and describe what changes at each boundary. |
| Clip Selection Agent | `clip_selection_agent` | `analysis` | specialist | Pick the segments worth keeping and say why each earns its place. |
| Subtitle Generation Agent | `subtitle_generation_agent` | `copy` | specialist | Turn a transcript into readable subtitle cues without changing meaning. |
| Subtitle Timing Agent | `subtitle_timing_agent` | `analysis` | router_light | Align cue timings to the audio and keep them readable. |
| Audio Cleanup Agent | `audio_cleanup_agent` | `analysis` | router_light | Decide which deterministic audio filters the clip needs, and why. |
| Video Transcoding Agent | `video_transcoding_agent` | `analysis` | router_light | Choose the encoding settings for the target platform and verify the output. |
| Video Format Agent | `video_format_agent` | `analysis` | router_light | Produce the exact aspect ratio, duration and codec each platform expects. |
| Final Video Assembly Agent | `final_video_assembly_agent` | `analysis` | specialist | Assemble the chosen clips, audio and subtitles into one file and verify it. |

### Graphic Design and Visual Identity — `design_visual`

**Leader: Design Director** (`design_director`, `specialist` profile). Give the brand a consistent visual language and keep every asset readable.

| Specialist | Id | Output | Profile | Mission |
|---|---|---|---|---|
| Thumbnail Design Agent | `thumbnail_design_agent` | `copy` | specialist | Describe a thumbnail concept that is readable at feed size and honest about the content. |
| Cover Design Agent | `cover_design_agent` | `copy` | specialist | Design a cover that reads at grid scale and matches the brand system. |
| Typography Agent | `typography_agent` | `analysis` | specialist | Choose typefaces, sizes and hierarchy that stay readable on a phone. |
| Colour Palette Agent | `color_palette_agent` | `analysis` | router_light | Define a palette with contrast that survives compression and dark mode. |
| Visual Identity Agent | `visual_identity_agent` | `analysis` | specialist | Describe the identity system so any future asset stays recognisable. |
| Layout Design Agent | `layout_design_agent` | `copy` | specialist | Plan a layout with a clear reading order and safe margins. |
| Visual Consistency Agent | `visual_consistency_agent` | `review` | router_light | Check that a set of assets looks like it comes from the same brand. |
| Social Graphics Agent | `social_graphics_agent` | `copy` | specialist | Brief a small graphic that carries one message and fits the platform. |
| Accessibility Design Agent | `accessibility_design_agent` | `review` | router_light | Check contrast, text size and motion against accessibility basics. |
| Creative Review Agent | `creative_review_agent` | `review` | specialist | Judge whether the creative work does its job, and say exactly what to change. |

### Platform Adaptation — `platform_adaptation`

**Leader: Platform Director** (`platform_director`, `specialist` profile). Adapt content per platform with the real constraints of each platform, never one-size-fits-all.

| Specialist | Id | Output | Profile | Mission |
|---|---|---|---|---|
| Facebook Content Agent | `facebook_content_agent` | `copy` | specialist | Adapt the piece for Facebook's feed: length, tone, link behaviour and format. |
| Instagram Content Agent | `instagram_content_agent` | `copy` | specialist | Adapt the piece for Instagram Reels and feed, including caption and hashtag structure. |
| TikTok Content Agent | `tiktok_content_agent` | `copy` | specialist | Adapt the piece for TikTok's discovery behaviour and native tone. |
| Reels Optimisation Agent | `reels_optimization_agent` | `analysis` | specialist | Optimise duration, pacing and cover for Reels. |
| Shorts Optimisation Agent | `shorts_optimization_agent` | `analysis` | specialist | Optimise the piece for Shorts: title, duration and loop. |
| Platform Caption Agent | `platform_caption_agent` | `copy` | specialist | Rewrite one caption for each target platform without changing the claim. |
| Platform Metadata Agent | `platform_metadata_agent` | `analysis` | router_light | Prepare titles, descriptions, tags and settings per platform. |
| Platform Format Validation Agent | `platform_format_validation_agent` | `review` | router_light | Check duration, ratio, size and codec against each platform's stated limits. |
| Platform Policy Research Agent | `platform_policy_research_agent` | `analysis` | specialist | Identify policy constraints that affect this content, and mark what you cannot verify offline. |
| Cross-Platform Adaptation Agent | `cross_platform_adaptation_agent` | `report` | planner | Produce a set of platform variants that stay recognisably the same piece. |

### Publishing Operations — `publishing_operations`

**Leader: Publishing Director** (`publishing_director`, `specialist` profile). Prepare a publishing plan that a human can approve, and record exactly what would happen.

| Specialist | Id | Output | Profile | Mission |
|---|---|---|---|---|
| Publishing Preparation Agent | `publishing_preparation_agent` | `analysis` | specialist | Assemble everything a publishing job needs, and list what is missing. |
| Publishing Schedule Agent | `publishing_schedule_agent` | `plan` | specialist | Propose a schedule with a reason per slot and no invented analytics. |
| Account Health Agent | `account_health_agent` | `analysis` | router_light | Report whether each connected account is usable right now. |
| Publishing Validation Agent | `publishing_validation_agent` | `review` | router_light | Run the pre-publish checklist and fail it when something is missing. |
| Duplicate Detection Agent | `duplicate_detection_agent` | `review` | router_light | Detect whether this content was already published or scheduled. |
| Publishing Monitor Agent | `publishing_monitor_agent` | `analysis` | router_light | Report the current state of each publishing job and what changed. |
| Publishing Retry Agent | `publishing_retry_agent` | `analysis` | router_light | Decide whether a failed publish is retryable, and say why. |
| Publishing Failure Agent | `publishing_failure_agent` | `analysis` | router_light | Explain what failed in language a human can act on. |
| Publishing Record Agent | `publishing_record_agent` | `report` | router_light | Produce a complete record of what was prepared, approved and attempted. |
| Publishing Reconciliation Agent | `publishing_reconciliation_agent` | `analysis` | specialist | Reconcile unknown outcomes without assuming success. |

### Audience and Community — `audience_community`

**Leader: Community Director** (`community_director`, `specialist` profile). Understand what the audience is saying and draft replies a human would be happy to send.

| Specialist | Id | Output | Profile | Mission |
|---|---|---|---|---|
| Comment Analysis Agent | `comment_analysis_agent` | `analysis` | specialist | Summarise what commenters actually said, with representative examples. |
| Sentiment Analysis Agent | `sentiment_analysis_agent` | `analysis` | router_light | Assess sentiment per theme and flag where it is changing. |
| Audience Feedback Agent | `audience_feedback_agent` | `analysis` | specialist | Extract the actionable signal from audience feedback. |
| Community Insights Agent | `community_insights_agent` | `analysis` | planner | Describe how the community behaves and what that implies for content. |
| Response Drafting Agent | `response_drafting_agent` | `copy` | specialist | Draft replies a human would be comfortable sending, and never send them. |
| FAQ Generation Agent | `faq_generation_agent` | `report` | specialist | Build an FAQ from questions the audience actually asked. |
| Audience Engagement Agent | `audience_engagement_agent` | `plan` | specialist | Plan engagement that is genuine and not mass-automated. |
| Community Risk Agent | `community_risk_agent` | `review` | specialist | Identify reputational or safety risks in the conversation and escalate them. |
| Feedback Categorisation Agent | `feedback_categorization_agent` | `analysis` | router_light | Categorise feedback into a small, stable taxonomy. |
| Community Reporting Agent | `community_reporting_agent` | `report` | specialist | Produce a community report a busy human can read in two minutes. |

### Analytics and Growth — `analytics_growth`

**Leader: Analytics Director** (`analytics_director`, `planner` profile). Explain performance from real stored metrics and propose testable next steps.

| Specialist | Id | Output | Profile | Mission |
|---|---|---|---|---|
| Performance Analysis Agent | `performance_analysis_agent` | `analysis` | specialist | Explain performance using only stored metrics, and say what is missing. |
| Views Analysis Agent | `views_analysis_agent` | `analysis` | router_light | Explain where views came from and what that implies. |
| Retention Analysis Agent | `retention_analysis_agent` | `analysis` | specialist | Explain where viewers drop off and what to change in the edit. |
| Engagement Analysis Agent | `engagement_analysis_agent` | `analysis` | specialist | Interpret likes, saves, shares and comments against the content's intent. |
| Campaign Analysis Agent | `campaign_analysis_agent` | `analysis` | planner | Evaluate whether the campaign achieved its stated success condition. |
| Hashtag Performance Agent | `hashtag_performance_agent` | `analysis` | router_light | Evaluate hashtag outcomes where data exists, and say when it does not. |
| Audience Growth Agent | `audience_growth_agent` | `analysis` | specialist | Explain follower growth and its likely driver without overclaiming. |
| Content Comparison Agent | `content_comparison_agent` | `analysis` | specialist | Compare two pieces fairly and extract what to repeat. |
| A/B Testing Agent | `ab_testing_agent` | `plan` | planner | Design a test with one variable, a success metric and a stop rule. |
| Growth Reporting Agent | `growth_reporting_agent` | `report` | specialist | Produce a growth report that separates measurement from interpretation. |

### Quality and Governance — `quality_governance`

**Leader: Quality Director** (`quality_director`, `planner` profile). Protect the user: verify facts, rights, brand safety and platform policy before anything ships.

| Specialist | Id | Output | Profile | Mission |
|---|---|---|---|---|
| Content Quality Agent | `content_quality_agent` | `review` | specialist | Judge whether the content is good enough to publish and name the weakest part. |
| Factual Accuracy Agent | `factual_accuracy_agent` | `review` | specialist | Check every factual claim and mark unsupported ones. |
| Copyright Review Agent | `copyright_review_agent` | `review` | specialist | Flag copyright and licensing risk, and say what evidence is missing. |
| Duplicate Content Agent | `duplicate_content_agent` | `review` | router_light | Detect whether this content repeats something already published. |
| Brand Safety Agent | `brand_safety_agent` | `review` | specialist | Identify content that could damage the brand and say why. |
| Platform Compliance Agent | `platform_compliance_agent` | `review` | specialist | Check the content against platform rules, and flag what cannot be verified offline. |
| Subtitle Quality Agent | `subtitle_quality_agent` | `review` | router_light | Check subtitles for accuracy, reading speed and sync. |
| Video Quality Agent | `video_quality_agent` | `review` | router_light | Check resolution, bitrate, audio level and duration against the target spec. |
| Metadata Quality Agent | `metadata_quality_agent` | `review` | router_light | Check titles, descriptions, tags and settings are complete and consistent. |
| Final Approval Agent | `final_approval_agent` | `review` | planner | Issue the final internal verdict and the exact conditions attached to it. |

## Output contracts

| Contract | Required fields | Notes |
|---|---|---|
| `analysis` | summary, findings |  |
| `copy` | summary, items |  |
| `plan` | summary, steps |  |
| `review` | verdict, issues |  |
| `report` | summary, sections |  |
| `approval` | summary, action, target |  |

## Tools

Agents never choose tools at runtime. The engine picks them deterministically from the task type, checks the grant in the database, validates arguments against the tool schema, and passes the result in as data.

| Tool | Grants |
|---|---|
| `project.metadata.read` | chief, strategic_intelligence, research_intelligence, creative_writing, video_production, design_visual, platform_adaptation, publishing_operations, audience_community, analytics_growth, quality_governance, read-only (all agents) |
| `content.metadata.read` | chief, strategic_intelligence, research_intelligence, creative_writing, video_production, design_visual, platform_adaptation, publishing_operations, audience_community, analytics_growth, quality_governance, read-only (all agents) |
| `content.variants.list` | platform_adaptation, analytics_growth |
| `media.inspect` | video_production, design_visual, quality_governance |
| `transcript.read` | video_production, platform_adaptation, audience_community, quality_governance |
| `brand.rules.read` | strategic_intelligence, creative_writing, design_visual, platform_adaptation |
| `internal.search` | chief, strategic_intelligence, research_intelligence, creative_writing, video_production, design_visual, platform_adaptation, publishing_operations, audience_community, analytics_growth, quality_governance, read-only (all agents) |
| `caption.compose` | creative_writing, audience_community |
| `content.readiness.check` | publishing_operations, quality_governance |
| `publish.plan.generate` | publishing_operations |
| `report.generate` | chief, strategic_intelligence, research_intelligence, creative_writing, video_production, design_visual, audience_community, analytics_growth, quality_governance |
| `human.approval.request` | chief, publishing_operations |

