---
title: Kinetic Explainer 全自動動畫解說成片
created: 2026-08-09
status: ready_for_agent
label: ready-for-agent
owner: codex
reference: https://www.instagram.com/reel/DbcNK8asov8/
review: approved_after_three_code_aware_passes
---

# Kinetic Explainer 全自動動畫解說成片 PRD

## Problem Statement

目前 auto-edit 已有可讀的 structured cards、三種視覺 pack、四種內容動畫、renderer evidence 與 final QA，但「丟入一支口播就自動產出高密度動畫解說片」仍不成立。中英字幕、opening title、動畫圖卡雖各有部分能力，但分散在 Studio、advanced workflow 或額外旗標下；section title、場景型動畫組版、動畫對應 SFX 規劃、多軌混音與 SFX 交付 QA 尚未存在。

使用者要的不是更多「隨機卡片」，而是一個會讀內容、切章節、選場景、選動畫、配對音效、產生中英字幕並自我驗證的導演系統。產品需要借用參考片的節奏語法，但不複製對方的版面、品牌、文案或音效資產。

## Reference Decomposition

參考 Reel 為 80.804 秒、1080×1920、30fps。離散抽格、場景變化與音訊 onset 分析顯示：

- 開場以多畫面 montage 快速建立視覺標準，後接 A-roll 解說。
- 設計段落常用「上方動畫場景＋下方講者」組版，並與全畫面 A-roll 交替，不是全程蓋卡。
- 視覺語彙包含 title reveal、staggered list/checklist、bar/ring/line dashboard、number counter、template mosaic、token meter、calendar/grid fill、font/color swap、typed prompt 與 dot progress。
- 中文主字幕與英文次字幕共用同一時間軌；重點字與章節視覺不取代字幕。
- 主要場景／動畫切換附近有密集瞬態音訊，而非單一音效無差別重複。
- 約每 4–8 秒出現一個主要視覺轉換，視覺場景內再以 0.3–1.2 秒的微動畫維持節奏；仍保留 A-roll 呼吸段。

## Solution

新增一個 `kinetic-explainer` 導演 profile。當使用者用自然語言要求「像參考片的動畫解說風格」或明確選擇此 profile 時，對單一本機口播影片自動完成：

1. 本機轉錄與語意章節分段。
2. 中文主字幕、英文次字幕與 sparse emphasis。
3. opening title、section title、場景型動畫圖卡與 A-roll 呼吸段的自動編排。
4. 依動畫家族與語意重要性選擇同一套原創本機 SFX pack，產生可追蹤的音效事件。
5. 以對白優先原則進行多軌混音、音量限制與 final loudness normalization。
6. 將字幕、動畫、SFX、資產來源、時序與 final output 綁定到同一份交付 receipt，失敗時不發布成片。

使用者不需分別開啟 translation、cards 或 SFX。選擇 profile 就是選擇整套交付契約。其他既有 director profile 維持原行為。

「全自動」指每支影片在一次選擇 profile 後不再要求逐項設定或中途確認。若英文翻譯只能由會傳送文字的 provider 完成，使用者必須先有一次性的明確 provider 同意；沒有有效同意時在 project mutation 前 fail closed，不以自動化名義偷送字幕。

## User Stories

1. As a creator, I want to provide one local talking-head video and choose one style, so that I receive a finished motion-rich MP4 without configuring individual effects.
2. As a creator, I want the system to understand semantic chapters, so that graphics explain the current idea instead of decorating random sentences.
3. As a creator, I want an opening title to appear automatically, so that the viewer understands the promise of the video immediately.
4. As a creator, I want section titles at real topic boundaries, so that a long explanation remains easy to follow.
5. As a creator, I want full-screen A-roll breathing room, so that constant graphics do not exhaust the viewer.
6. As a creator, I want a split graphic/presenter stage when an explanation benefits from visuals, so that the speaker and the explanation remain visible together.
7. As a creator, I want multiple animation families, so that every section does not look like the same card fading in.
8. As a creator, I want charts and statistics to remain transcript-grounded, so that animations never invent numbers or claims.
9. As a creator, I want Chinese and English subtitles on the same timing rail, so that both audiences can follow the same spoken moment.
10. As a creator, I want English translations to be concise rather than literal, so that the second line stays readable on a phone.
11. As a creator, I want translation coverage to fail closed, so that a bilingual profile never quietly ships Chinese-only captions.
12. As a creator, I want meaningful words emphasized sparsely, so that emphasis guides attention instead of animating every word.
13. As a creator, I want animation-specific sound cues, so that title, list, count, grid, prompt and transition motions each feel intentional.
14. As a creator, I want one coherent SFX pack per clip, so that the soundtrack does not sound like unrelated random samples.
15. As a creator, I want dialogue to stay dominant, so that sound design never masks the speaker.
16. As a creator, I want every external or generated audio asset to have provenance, so that a final delivery has a defensible rights record.
17. As an editor, I want audio events to reference the resolved visual event that triggered them, so that changing an animation invalidates stale sound cues.
18. As an editor, I want SFX events to remain editable in Studio, so that exceptional timing or gain can be adjusted without rebuilding the whole plan.
19. As an editor, I want animation and sound density controls, so that the same profile can be restrained or energetic without changing its design identity.
20. As an editor, I want a preview receipt that reports expected and delivered scenes, captions and cues, so that missing work is visible before final render.
21. As an editor, I want final QA to reject out-of-range, overlapping or silent SFX events, so that metadata cannot claim delivery that the mix did not contain.
22. As an editor, I want final QA to preserve the existing font, visual-evidence, black-frame, audio-dropout and loudness gates, so that new polish does not weaken prior guarantees.
23. As a local-first user, I want transcription, planning, rendering and the default SFX pack to stay local, so that the source video is not uploaded.
24. As a local-first user, I want an explicit consent boundary before any online translator is used, so that bilingual output does not silently send text to a cloud service.
25. As a maintainer, I want old director profiles to keep their existing behavior, so that the new premium profile does not break conservative workflows.
26. As a maintainer, I want one primary renderer and one resolved timeline, so that visual and audio producers cannot create competing sources of truth.
27. As a maintainer, I want deterministic plans and generated SFX assets, so that identical inputs and settings produce equivalent receipts.
28. As a reviewer, I want three real-media goldens and mutation tests, so that a green unit suite is not mistaken for a convincing finished video.

## Implementation Decisions

### Product interface

- The stable v1 render invocation is `auto_edit.py cut --input INPUT --out OUT --director kinetic-explainer`. The public read-only preflight is `auto_edit.py resolve-director --director kinetic-explainer`; it invokes the same resolver without creating a project or output.
- The CLI, Studio and agent-first natural-language adapter must all call one canonical resolver and select this exact profile ID rather than a private orchestration branch. After project creation, the resolver result is stored at `working/resolved_director_profile.json`.
- The agent adapter emits a schema-v1 selection request containing `profile_id`, the explicit user style phrase/reference intent and requested compatible overrides; it does not emit private capability flags. Explicit phrases such as「動畫解說／動畫圖卡＋中英字幕＋配對音效」map to `kinetic-explainer`; ambiguous “幫我剪好看” requests retain the existing default instead of silently opting into translation or cloud consent.
- The normalized request is persisted after project creation at `working/director_selection_request.json` with `schema_version`, `profile_id`, `selection_reason`, normalized evidence, compatible overrides and the resolver-returned `resolved_profile_hash`. `selection_reason` is one of `explicit_profile`, `explicit_kinetic_bundle`, `reference_style_match` or `default_unchanged`. The public preflight also accepts `--selection-request REQUEST_JSON` so this seam is testable without relying on private agent state.
- Upgrade the director registry contract so this profile resolves a schema-versioned experience envelope: required bilingual caption delivery, English target, translation provider/consent policy, scene pack, style pack, stage layout, visual density, motion intensity, SFX mode, SFX pack and cue-coverage policy. Runtime consumers may not discard the registry version, experience fields, rules or resolved hash.
- The resolved artifact contains `schema_version`, `resolver_version`, `profile_id`, `registry_schema_version`, `registry_entry_version`, `experience`, `overrides`, `required_capabilities` and `resolved_hash`. `resolved_hash` is SHA-256 of canonical UTF-8 JSON excluding the hash field, with recursively sorted keys, no insignificant whitespace and no non-finite numbers.
- Resolution precedence is: hard safety/approval constraints, explicit compatible user overrides, profile experience envelope, style-pack tokens, component defaults. Compatible v1 overrides are input/folder/main/out/project-dir, clips/seconds, platform, brief, glossary/fix, keep-pauses, framing, quality and model. `--translate en` and `--cards-from-model` are accepted as redundant confirmations. A non-English `--translate`, `--no-cards`, `--no-editorial` or `--burned-in yes` conflicts with required kinetic delivery and must fail; unrecognized experience overrides also fail rather than being dropped.
- Successful resolution exits 0 and emits the canonical JSON. Resolution failure exits 2 with stable JSON codes `unknown_director`, `registry_invalid`, `profile_conflict` or `capability_missing`, plus sorted conflict/missing-capability fields. `cut` performs this preflight after validating the input path but before project initialization or output mutation.
- `required_capabilities` includes an approved translation provider and consent-reference hash. A fully local provider needs no network consent; a cloud provider requires a previously persisted, provider-specific consent record. Provider/consent is resolved before the run, never prompted or silently switched mid-run, and is recorded in caption v2 and the final envelope.
- A CLI/Studio/agent resolver hash mismatch is a `profile_conflict`. Preview may produce preview artifacts, but only `--quality final` can finalize a delivery envelope.
- Natural-language agent-first requests may select this profile. The public success result is an MP4 plus a finalized delivery envelope, QA report and contact sheet, not a plan-only response.
- The profile is fail-closed. Missing translation, unresolved visual evidence, missing SFX assets, stale cue bindings or failed mix QA stop final publication. Other existing profiles preserve their current fallback rules.

### Deep modules

- **Kinetic profile resolver:** resolves the complete schema-versioned experience envelope behind one stable registry interface and emits a deterministic profile revision/hash consumed by CLI, Studio, planner, renderer and receipts.
- **Motion scene planner:** converts transcript-grounded semantic beats into opening, section, A-roll and graphic-scene roles. It extends the existing visual plan rather than creating a second competing timeline.
- **Audio event planner:** consumes the resolved visual/motion plan and returns deterministic SFX events. Each event references its visual trigger and the resolved motion-plan hash.
- **SFX catalog:** owns asset identity, semantic tags, duration, transient anchor, measured loudness/peak, provenance, license/review state and pack membership.
- **Timeline audio mixer:** accepts source dialogue plus final-domain audio events, applies gain/fades/dialogue-priority ducking, mixes once, emits a hash-bound SFX stem for QA, and returns observed delivery evidence.
- **Caption delivery contract v2:** addresses captions by stable caption ID, not source text, and binds source hash, source/final timing, timeline revision, target language, provider/consent identity and translation to the renderer receipt.
- **Delivery envelope:** owns per-output hashes and finalization state across direct, single, variant and batch routes.
- **Unified delivery QA:** independently compares expected and delivered visual scenes, sampled motion, caption bindings and audio-event/stem evidence before any final output is exposed.

### Visual scene vocabulary

The first production vocabulary contains seven reusable families:

1. Hero or section title reveal.
2. Staggered list/checklist.
3. Analytics dashboard with bar, ring and line motion.
4. Count/stat scene with a completion accent.
5. Mosaic/showcase scene for screenshots or owned assets.
6. Grid/calendar/progress fill scene.
7. Typed prompt/command scene.

Every family has a static semantic fallback for preview, but `kinetic-explainer` final delivery requires faithful high-motion evidence. Layout supports both full-screen graphics and an upper graphic stage with the presenter retained below.

### SFX system

- Do not extract or reuse sounds from the reference Reel. Ship an original, procedurally generated local starter pack with at least: soft UI tick, short pop, short whoosh, soft impact, short riser, typing tick and completion chime.
- Map animation families to cue roles, not hard-coded filenames. A pack resolves roles such as `title_enter`, `row_reveal`, `count_tick`, `transition`, `grid_fill`, `typing` and `complete` to assets.
- `audio_event_plan.json` is the audio source of truth and uses the final 48kHz timeline domain. Sample 0 is the first decoded sample of the post-cut/reorder output. Seconds or rational frame times are converted once with round-half-up to integer samples; the integer value is authoritative thereafter. Each event records ID, timeline revision, cut-map hash, trigger ID, trigger onset sample, event start/duration samples, asset ID, asset transient-anchor sample, role, gain, fades, duck group, evidence/reason and review state.
- Audio-event planning runs only after cuts, reordering and resolved visual timing. Any change to the timeline revision, cut map, trigger onset or motion-plan hash makes the audio plan stale and blocks render.
- The catalog-build noise floor is the 10th percentile of 5ms max-channel-RMS windows. The transient anchor is the earliest 5ms window crossing `max(-45 dBFS, noise_floor + 12 dB)`; ties choose the earliest window and an asset with no crossing is invalid. Expected transient sample is `event_start_sample + asset_transient_anchor_sample`. The planner targets the trigger onset; at the timeline boundary it may choose the earliest feasible event start only when expected-to-trigger error remains within 3,840 samples.
- The mixer emits a deterministic QA stem as 48kHz stereo PCM `s24le` WAV. The envelope binds both exact-file SHA-256 and SHA-256 of decoded interleaved PCM bytes. Delivered transient detection applies the same 5ms/max-channel-RMS rule in the stem within `[expected-3840, expected+3840]`; ties choose the earliest crossing. Measurement-only windows are zero-padded at output boundaries, but an event or its non-fade payload outside output bounds fails before mix rather than being clamped.
- Cue alignment is the absolute sample difference between detected stem transient and the bound trigger onset; expected transient must also be present in the detector window. The v1 tolerance is 3,840 samples at 48kHz (80ms). Deleted/reordered triggers, resampling drift or missing transients fail.
- Apply density limits and de-duplication. Adjacent cue onsets are at least 120ms apart, no more than two cues may overlap, and clip-wide density is at most 40 planned cues per minute.
- Dialogue remains the priority track. Final mixing uses one audio graph and one final loudness pass; new SFX must not cause double normalization.

### Normative visual-plan fields

- Every resolved scene/trigger records `eligibility`, `eligibility_reason`, `family`, `role`, `importance`, `major_graphic`, `micro_silent`, `motion_window_start_sample`, `motion_window_end_sample`, normalized `graphic_roi`, normalized `presenter_roi` and `trigger_role`. The canonical plan hash freezes these values before audio planning.
- A family is `eligible` only when its schema-valid transcript/project evidence and required licensed asset references resolve. `ineligible` requires one enumerated reason: `missing_transcript_evidence`, `unsupported_payload`, `missing_licensed_asset`, `density_budget` or `layout_collision`; arbitrary prose cannot hide an omitted family.
- `major_graphic=true` means the declared graphic ROI covers at least 15% of the frame for at least 0.8 seconds. A-roll breathing is final time with no delivered major graphic. A visual-change gap over 12 seconds is explained only by a plan interval explicitly labeled `a_roll_breathing`; otherwise it fails.
- `micro_silent=true` is allowed only for low-importance row/typing repetitions. It is forbidden for opening/section titles, scene transitions, count/chart/grid completion and other semantic completion triggers. Cue eligibility consumes these frozen fields; the renderer cannot reclassify them after planning.
- A motion window must have positive duration, lie inside its scene and be represented by final-domain samples. Renderer evidence must match the frozen family, role, ROI and window before sampled-frame motion can pass.

### Bilingual captions

- `kinetic-explainer` defaults to Traditional Chinese primary captions and concise English secondary captions.
- `kinetic-explainer` requires caption-delivery artifact v2; the legacy source-text lookup remains backward-compatible for other profiles but is not accepted here.
- Raw ASR first produces immutable `working/transcript_source_revision.json`, before glossary fixes, editorial corrections or readable-caption chunking. Its canonical payload contains `schema_version`, `source_media_sha256`, selected audio-stream index and decoded-audio PCM hash, transcription engine/version/model/language, canonical decoding parameters, `source_generation` and the ordered raw word stream `{source_word_index,start_us,end_us,text,speaker}`. `transcript_source_revision` is SHA-256 of canonical UTF-8 JSON excluding the revision field, using the same sorted-key/no-whitespace/non-finite rejection rules as profile hashing.
- Normal runs reuse this artifact when media, decoded audio and ASR identity/parameters match. An explicit forced re-transcription increments the project-local `source_generation` before ASR and therefore mints a new revision even if words happen to match; changed media, engine/model/parameters or raw word boundaries/text also mint a new revision. Later text corrections never rewrite this artifact.
- The authoritative source-caption artifact is the corrected/re-chunked `caption_segments` in `working/transcript_words.json`, before cut mapping, and carries its source revision plus `caption_segmentation_revision`. The segmentation revision hashes chunker name/version/config and the canonical ordered source-word spans. A changed chunking algorithm/config or changed spans mints a new segmentation revision; rerunning the same chunker/config on the same spans is stable.
- Caption IDs are minted as `caption-<16 hex>` from `transcript_source_revision`, `caption_segmentation_revision`, inclusive source-word-index span and within-span ordinal. Text-only fallback segments use both revisions, quantized source start/end and ordinal. A correction that leaves the source span and chunking unchanged preserves the ID but changes the per-item corrected-source hash, invalidating its translation; forced re-transcription or re-chunking changes IDs.
- A cut/reorder creates a unique final `caption_instance_id` from source caption ID plus deterministic occurrence ordinal. Artifact v2 binds both IDs, the canonical whole-source-caption-list hash, a per-item source hash, source/final timing, cut-map/timeline revision, target language, provider identity, consent mode, translation status and translated text.
- Renderer lookup is by unique non-empty `caption_instance_id` and matching revisions, so duplicate Chinese lines cannot overwrite each other. Missing, extra, duplicate-ID, stale-source, stale-timeline, order or timing mismatch blocks before render and again before publication.
- Translation is one-to-one, ordered and complete; names, numbers, units and claims are preserved. A fully unchanged item fails unless `translation_status=identity_preserved` and `identity_reason` is one of `brand`, `proper_name`, `code` or `number_unit`. That exception is item-scoped and cannot justify leaving a Chinese sentence untranslated.
- The English line uses a smaller design token and never creates a third subtitle line. Long English is shortened semantically or the caption chunk is rebalanced before rendering.
- Translation uses the configured approved provider. A fully local provider is preferred; a cloud provider is permitted only with a prior provider-specific consent record and is never silently selected or substituted. Missing provider/consent fails profile preflight before project mutation.

### Planning and pacing

- Major visual beats are transcript-grounded and budgeted by duration. A 45–90 second clip targets roughly one major graphic scene every 4–8 seconds, with A-roll breathing segments between dense sequences.
- Micro-motion lives inside a selected scene; it does not inflate the semantic card count.
- Opening and section titles must describe transcript-backed ideas. Numeric scenes require exact evidence. Asset mosaics require owned/project assets and provenance.
- The planner records why each scene and cue was chosen. Changing transcript, timing, layout, motion or assets invalidates downstream caption/audio/final receipts.

### Delivery contracts

- Extend renderer evidence with expected/delivered scene role/family, final start/end, graphic ROI/stage bounds, asset provenance, static-fallback state, motion probe frames and audio-event delivery evidence.
- A per-output schema-versioned delivery envelope binds route/render ID, `prepared|finalized` state, profile hash, timeline/cut-map hashes, output hash, QA/contact hashes, visual/motion evidence hash, caption-v2 hash, audio-plan/catalog/stem hashes and renderer identity. Canonical finalized envelopes live at `working/delivery_envelopes/<render_id>.json`; prepared envelopes and candidates live under `working/delivery_envelopes/.staging/<render_id>/` and are never public success.
- **Direct final, first owned route:** render beside the private staging envelope; produce observed evidence, QA and contact sheet; create and validate a prepared envelope; preserve any prior destination; publish the candidate; then atomically write the finalized canonical envelope. If finalization fails, restore the prior destination or remove the new one and leave no finalized envelope. Exit 0 requires both matching output bytes and finalized envelope.
- **Agent-first cut:** consumes the direct renderer's finalized envelope, removes the later unbound QA call, and appends a clip to its success list only after envelope/output hashes match. Failure exits 2; a new output is absent, a previous output SHA remains unchanged, and no new finalized envelope exists.
- **Server single:** render a private candidate and prepared envelope, validate them, publish the output, finalize the envelope, and only then update `render_status` and the downloadable pointer. Failure keeps the previous public pointer/output/envelope unchanged.
- **Variant:** follows the same sequence with a per-variant render ID. A current variant is exposed only after its finalized envelope matches its output.
- **Batch:** requires every member to have a finalized envelope, builds the ZIP privately, and creates an aggregate envelope binding ordered member render IDs, member/output hashes, ZIP member paths/hashes and ZIP hash. The ZIP and aggregate envelope become public only together; one failed member preserves the previous batch publication and exposes no partial replacement.
- Filesystems cannot atomically replace an MP4 and JSON as one operation, so public state is defined by the matching finalized envelope/pointer, not file existence alone. Startup/retry cleanup removes stale prepared files/candidates, restores a preserved prior output when possible, and quarantines an unmatched output before any route reports success.
- Every route has one observable mismatch mutation. Assertions are route-specific but share the invariant: nonzero status, no new public pointer/finalized envelope, and either no new output or the exact prior output SHA.

## Acceptance Criteria

### Tracer bullets

- **P0a selector:** the stable cut invocation accepts `kinetic-explainer`, CLI and Studio resolve the same schema/profile hash, and an unsupported or contradictory selector fails before render. Until required downstream capabilities exist, the selected profile fails with an explicit capability-preflight result instead of silently degrading.
- **P0b delivery:** direct final rendering of one existing visual produces a finalized per-output envelope. Mutating the output, QA hash or renderer evidence prevents successful publication and preserves any prior destination.
- **P0c captions:** one 15–25 second fixture delivers caption artifact v2 with duplicate source text at two IDs and two correct English lines. Removing, duplicating, changing or staling either binding prevents final publication.
- **P0d SFX:** one existing faithful visual trigger produces one generated SFX cue in the final 48kHz domain and a hash-bound SFX stem. Deleting/reordering the trigger, shifting onset beyond 3,840 samples or replacing the asset with silence prevents final publication.
- **P0e integrated:** after P0a–P0d are green, one public `kinetic-explainer` command produces a real MP4 with bilingual captions, one title scene, one faithfully animated explanatory scene, one synchronized SFX cue and a finalized unified envelope.

### Visual delivery

- The three acceptance fixtures are curated to contain at least four independently annotated evidence roles: opening promise, enumerated/list content, numeric/stat content and one owned visual asset. Each 45–90 second output must contain an opening scene, at least two section/graphic scenes and at least four distinct delivered visual families without fabricating missing evidence.
- For production inputs outside those fixtures, the planner records the normative eligible/ineligible fields and enumerated reasons defined above; QA verifies the frozen resolved-plan hash and delivery rather than forcing an unsupported family.
- Major scene count and family IDs match the resolved plan. High-motion evidence records `static_fallback=false`, final bounds and a graphic-only ROI. QA samples 10%, 50% and 90% of the declared motion window; at least one pair must have ROI SSIM below 0.985 and at least 2% changed pixels after excluding the presenter region.
- For outputs over 45 seconds, 25–55% of duration is frozen-plan A-roll breathing time with no delivered `major_graphic`, including at least two continuous intervals of 2 seconds or more. Any interval over 12 seconds without a declared scene start/end or motion-window sample must be wholly covered by an explicit `a_roll_breathing` interval or fail.
- Structured text respects the current 32px final-output floor and platform safe areas.

### Caption delivery

- Every visible source caption in the bilingual profile has exactly one non-empty English translation on the same timing rail.
- Source and instance IDs are unique and non-empty. Translation set, order, whole-source artifact hash, per-item source hash, source/final timing and revisions match the final caption receipt; unchanged or missing lines fail except for the enumerated identity-preserved cases.
- Contact-sheet inspection shows no three-line subtitles, title/caption collisions or safe-area violations.

### SFX delivery

- Every planned SFX event references a delivered visual trigger, an approved/generated catalog asset and valid 48kHz final-timeline samples.
- Eligible cue roles are frozen-plan, faithfully delivered high-motion `title_enter`, `scene_transition`, `row_reveal`, `count_complete`, `chart_complete`, `grid_complete` and `typing` triggers with `eligibility=eligible` and `micro_silent=false`. At least 70% receive a planned cue and 100% of planned cues must be detected in the SFX stem.
- Catalog measurement decodes assets to 48kHz stereo float samples. Asset RMS is the greater per-channel RMS over the full non-padded asset; sample peak is maximum absolute sample across channels. Assets are 40ms–1.5s, RMS above -45dBFS and sample peak between -12 and -1dBFS. QA rejects a missing transient anchor, silence or out-of-policy asset before mixing.
- Observed cue alignment uses the normative transient formula/detector and is within 3,840 samples after cuts/reordering. In a zero-padded 250ms bound stem window centered on expected transient, max-channel sample peak is at least -42dBFS; expected/delivered count, cue IDs, exact-file hash and decoded-PCM hash match.
- Dialogue/SFX comparison decodes both 48kHz stereo stems and computes per-channel RMS in the same 250ms zero-padded window, using the louder channel for each stem. When dialogue RMS is above -45dBFS, SFX RMS must remain at least 6dB below it. Final audio also stays within the existing integrated loudness, true-peak and dropout policy. Subjective masking remains a recorded human audio spot-check, not a falsely precise automated claim.

### Regression and truthfulness

- Existing director profiles, approvals, visual font/motion evidence, receipt binding and final publication gates continue to pass their current suites.
- At least three real talking-head videos of different pacing are rendered. Each receives automated reports plus human contact-sheet and audio spot-check review.
- Mutation tests cover missing translation, stale trigger hashes, count drift, out-of-range cues, duplicate cue IDs, silent SFX, excessive cue overlap, fallback motion and all four final publication routes.

## Phase Plan

### Phase 0a — Canonical public selector

- Public command: `python3 skills/auto-edit-video/scripts/auto_edit.py resolve-director --director kinetic-explainer`.
- Fixture state: valid v1 registry entry with all required capabilities declared; no project/output exists.
- Expected artifacts: canonical JSON on stdout only, deterministic `resolved_hash`, exit 0 and zero filesystem mutation. A second invocation uses `--selection-request EXPLICIT_KINETIC_REQUEST.json`; its normalized adapter selection, Studio response, resolver stdout and later persisted `director_selection_request.json`/`resolved_director_profile.json` must expose the exact same profile hash and `selection_reason`.
- Failure mutation: pass `--no-cards` through cut preflight. Expect exit 2 with `profile_conflict`, sorted conflict fields, no project/output creation and no renderer invocation.

### Phase 0b — Direct finalized delivery envelope

- Public command: `python3 skills/auto-edit-video/scripts/render_editor_timeline.py --project-dir FIXTURE_PROJECT --output OUT --quality final`.
- Fixture state: existing approved single-visual project and any prior `OUT` SHA recorded.
- Expected artifacts: output MP4 plus finalized `working/delivery_envelopes/<render_id>.json` binding output, QA, contact and visual evidence hashes; no staging residue.
- Failure mutation: change the candidate output after prepared-envelope hashing. Expect nonzero exit, no new finalized envelope and absent new output or the exact prior `OUT` SHA.

### Phase 0c — Caption-delivery artifact v2

- Public command: `python3 skills/auto-edit-video/scripts/auto_edit.py translate-captions --project-dir FIXTURE_PROJECT --language en --required`.
- Fixture state: 15–25 second project whose authoritative source artifact contains two identical Chinese strings under distinct source spans/IDs and a frozen cut map.
- Expected artifacts: immutable transcript-source revision, caption-segmentation revision, caption v2 with unique source/instance IDs, two correct English items, source/timeline hashes and provider/consent receipt; renderer consumes it by instance ID. A correction-only rerun preserves caption IDs while changing corrected-source hashes; forced re-transcription and changed re-chunking each mint new IDs and stale the old translations.
- Failure mutation: remove one instance ID before final render. Expect `caption_binding_missing`, no new output and no finalized delivery envelope.

### Phase 0d — One SFX cue and final-domain timebase

- Public command: `python3 skills/auto-edit-video/scripts/render_editor_timeline.py --project-dir ONE_CUE_PROJECT --output OUT --quality final`.
- Fixture state: single-cut approved project with one frozen faithful motion trigger and one valid generated catalog asset.
- Expected artifacts: `audio_event_plan.json`, 48kHz stereo PCM `s24le` QA stem, catalog/plan/stem evidence and finalized hashes; detected transient is within 3,840 samples of expected.
- Failure mutation: shift the decoded stem transient by 3,841 samples. Expect nonzero exit, prior output preservation and no new finalized envelope. A separate deleted/reordered-trigger mutation is required before claiming reorder support.

### Phase 0e — Integrated public tracer

- Public command: `python3 skills/auto-edit-video/scripts/auto_edit.py cut --input REAL_FIXTURE --out OUT_DIR --director kinetic-explainer --clips 1 --quality final`.
- Fixture state: approved 15–25 second real talking-head media, approved translation provider/consent reference configured and all P0 capabilities present.
- Expected artifacts: one MP4, resolved profile, caption v2, visual/motion evidence, audio plan/catalog/stem, QA/contact sheet and one matching finalized envelope; the cut success list contains exactly that output.
- Failure mutation: stale the caption timeline revision after planning. Expect exit 2, empty success list, no new output and no new finalized envelope.

### Phase 1 — Audio event deep module

- Expand the Phase 0d one-cue slice into the full SFX catalog, deterministic local starter pack, role mapping, density policy, mixer evidence and event QA while preserving its final-domain timebase contract.
- Expose audio events in Studio without making Studio mandatory for agent-first delivery.

### Phase 2 — Scene director and layout vocabulary

- Add semantic chapter boundaries, section titles, split graphic/presenter stage and the seven visual families.
- Reuse existing style tokens, structured-card geometry, animator and visual evidence instead of introducing a second renderer.

### Phase 3 — Bilingual presentation and language quality

- Add mobile type tokens, English shortening/re-chunking, line-height rules and contact-sheet collision checks on top of the Phase 0c delivery contract.
- Add terminology/name/number preservation mutations and real bilingual review; do not defer identity/revision binding to this Phase.

### Phase 4 — Final integration and real-media acceptance

- Extend the Phase 0b envelope from direct to single, batch and variant routes; batch binds member order, member hashes and ZIP contents. Each route adds its exact prepared/finalized/public-pointer crash and mismatch tests before being declared supported.
- Run full regression, mutation suite, three real-media renders, contact sheets and audio spot checks.

Each Phase uses one RED→GREEN vertical slice at a time and ends with an independent rollback checkpoint. Non-trivial completed work receives fresh-context verification before delivery.

## Testing Decisions

- Prefer public-interface integration tests. Every Phase 0 slice above names its public command, fixture state, exact expected artifacts and one isolated no-publication mutation; P0e calls the same agent-first entry point a user invokes.
- Resolver unit tests also freeze canonical JSON/hash vectors, exit codes and the complete override matrix. A registry parser that drops version, experience, rules or hash must fail these vectors.
- Delivery crash tests interrupt each stage: before prepared envelope, after prepared validation, after output replacement and before/after finalized envelope/pointer. A file existing without a matching finalized envelope is never asserted as successful publication.
- Caption v2 tests freeze transcript-source and caption-segmentation canonical hash vectors and cover ID lifecycle across correction-only edits, forced re-transcription, changed re-chunking, duplicate source text, repeated cut occurrences and reorder; only enumerated identity-preserved items may equal source text.
- SFX tests freeze resampling, round-half-up sample mapping, transient-anchor detection, exact/decoded PCM hashes, channel aggregation, zero-padding and earliest-crossing tie-break behavior.
- Keep schema validation tests for SFX catalogs, audio event plans and caption receipts, but do not mistake schema shape for rendered delivery.
- Test the audio event planner as a deep module using resolved public visual-plan inputs and observable event outputs.
- Test the mixer with real generated WAV assets and FFmpeg output. Verify 48kHz sample timing, SFX-stem transient alignment, stream presence, duration, dialogue/SFX ratio, loudness/peak and bound evidence rather than mocking FFmpeg.
- Test every scene family with a real renderer smoke output and evidence receipt. QA independently samples the declared graphic ROI; golden images may catch design drift, while semantic assertions cover transcript grounding and safe areas.
- Reuse existing final-path publication tests for direct CLI, single, batch and variant routes, then add caption/audio receipt mutations to each relevant route.
- Real-media acceptance must distinguish mechanical QA from human review. Mechanical pass is necessary; contact-sheet and audio spot-check evidence is recorded separately.

## Out of Scope

- Pixel-for-pixel cloning of the reference creator's design, text, logo, templates or audio assets.
- Downloading commercial SFX libraries or using unlicensed music.
- Automatic background music selection in the first release. The architecture may reserve a music bus, but this PRD delivers dialogue plus SFX.
- Voice cloning, replacement narration or automatic publishing to Instagram.
- Fabricated charts, product screenshots or numeric claims not grounded in transcript/project assets.
- Replacing the existing primary renderer or introducing a second editable timeline.
- Removing human approval gates from Studio workflows; agent-first delivery remains governed by its existing authorization and final QA contract.

## Further Notes

- The reference video and analysis artifacts are temporary local research inputs only and must not be committed or redistributed.
- The current repo already has strong visual delivery gates. New audio/caption contracts must extend those guarantees rather than create a parallel pass/fail mechanism.
- Historical project notes were used only as routing and review checklists; all current capability statements in this PRD were re-checked against the present codebase.
- No issue tracker or triage-label target was configured for this workspace, so this PRD is stored locally and must receive a fresh-context code-aware review before its status changes to `ready_for_agent`.
