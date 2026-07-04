# Dialogue & Motion Authoring — Constraints

You are authoring dialogue and motion for a video_maker project through this MCP.
Read context first (`get_project`, `get_shot`/`list_shots`), then follow EVERY
constraint below. These are the same constraints the script generator works to —
treat them as authoring rules, not suggestions.

## 1. Dialogue (`text` / 台词)

- **Language:** write in the SAME language as the project theme / existing
  dialogue. Never switch languages mid-project.
- **Word count — count the words BEFORE you submit and stay within range:**

  | shot_duration | word target |
  |---|---|
  | 4s | 8–10 |
  | 6s | 13–16 |
  | 8s | 18–21 |

  The range is set by how much speech fits the shot's length (~2.6 words/sec).
  **Over the maximum can't be spoken in time; under the minimum leaves dead air.**
  If a line won't fit, tighten or cut it — do NOT exceed the maximum. Counts are
  English-word approximations; for CJK, match the comparable spoken length.
- **Voice:** natural, in the character's voice (personality is implied by the
  character reference images). Never submit empty text.

## 2. Motion (`motion_prompt` / 动作)

- Write the motion prompt in **ENGLISH**, even when the dialogue is another language.
- Describe **camera movement + talking-head physiological cues** (gaze, gestures,
  blinking, lip movement); preserve visual fidelity to the reference / first frame.
- **Dialogue placement:** weave the dialogue INTO the action beats — each sentence
  inlined at its moment (e.g. `saying "..." as she flips the card`). This is the only
  way to align speech with actions; a bare trailing dialogue line has no timing
  context and makes the character start speaking at 0s. `sync_lip_marker` defaults
  to `false`; pass `true` only when you explicitly want the trailing
  `The character says: "..."` marker appended instead (never hand-write that line).
  After editing dialogue, update the inlined sentences to match.

### Motion-prompt authoring lessons (Veo 3.1, learned in production)

- **Intra-shot timing — use timestamp segments.** Prose timing rules ("for the
  first 2 seconds she is silent") are IGNORED; speech otherwise starts at 0s and
  fills the shot. Place actions and dialogue in time with the official segment
  syntax:
  ```
  [00:00-00:02] She slides one card out of the fan, its back to the camera, saying "and there it is."
  [00:02-00:05] Holding the card up, she says "six of cups, reversed."
  [00:05-00:08] She turns the card over toward the camera at the very last moment.
  ```
  If timestamps still fail, restructure across shots: move the pre-speech action
  to the END of the previous shot so this shot can speak from 0s, and/or pad the
  line's start with transition words ("and there it is.") to push key words later.
- **Keep prompts SIMPLE.** Long fidelity boilerplate + many-step sequences cause
  instruction drift (steps get skipped or merged). State the camera, the beats
  with inlined dialogue, and lip-sync — nothing else.
- **Complex action sequence and target last frame: pick ONE.** Frame
  interpolation rushes to the pinned end frame and compresses/skips intermediate
  steps. With a tail frame, keep the action to 1–2 steps; move extra steps to a
  neighboring shot.
- **Key props (card faces, text, logos) cannot be invented mid-shot.** Shot-level
  reference images are NOT sent to Veo when first/last frames are used (the API
  modes are mutually exclusive) — the model will hallucinate the prop from the
  dialogue words. Reveal the prop at the shot's very END, pinned by a target last
  frame containing the real image; the next shot inherits the correct picture via
  first-frame continuity.
- **Prefer positive phrasing** ("its back faces the camera the whole time") over
  "never/no" statements — negations are weakly followed.

## 3. Storyboard structure & status

- `replace_storyboard` requires the project in **`script_review`** status (else 409).
- Storyboard shots carry **structure + dialogue only — NO `motion_prompt`**. Set
  motion afterward via `update_motion` / `batch_update_shots`.
- **`shot_duration` must be 4, 6, or 8** — these are the only supported durations
  (they're the only ones with a word-count range and a valid generation length).
- Each shot: `shot_id` (unique), `text`, `shot_type`, `visual_description`,
  `shot_duration`, `align_with_previous`, `reference_image_hint?`.

## 4. First-frame continuity (informational)

- A shot's first frame is chosen at generation time: shot 1 → character reference
  image; connected shots → the previous shot's current last frame (continuity,
  reflects any trim). A manually set first frame overrides this.
- You don't set frames through this MCP — author `text`/`motion_prompt` only;
  keyframes are managed in the UI.

## 5. Reference materials

- Character/scene reference images and the reference voice (音色校准) are managed in
  the UI. Read them via `get_project`; you cannot change them here (except
  `upload_reference_images` when that tool is available). At least one character
  reference image is required before script generation runs.

## 6. 生命周期工具（状态机）

项目状态机与对应驱动工具：

```
draft ──start_generation──▶ scripting ──▶ script_review
script_review ──(replace_storyboard / batch_update_shots 编辑)──▶ script_review
script_review ──approve_script──▶ shot_generating ──▶ shot_review
shot_review ──(regenerate_shots / 编辑)──▶ shot_generating ──▶ shot_review
shot_review ──continue_generation──▶ shot_generating（续跑 pending/failed shot）
shot_generating ──cancel_generation──▶ shot_review（止损）
shot_review ──export（暂无 MCP 工具，走 UI）──▶ exporting ──▶ exported
```

- `start_generation` 前置：项目为 draft 且已用 `upload_reference_images` 上传 ≥1 张
  character 参考图。
- 驱动类工具（`start_generation` / `approve_script` / `regenerate_shots` /
  `continue_generation` / `cancel_generation`）为**异步触发**：只入队并立即返回
  `{status, message}`；进度用 `get_generation_status` 轮询（返回项目 status 与每个
  shot 的 status / has_video / video_path / error_message / vc_status / tf_status）。
- 非法状态下调用返回 `{"ok": false, "status_code": 409, "error": ...}`，属正常
  反馈而非连接错误。

## Pre-submit checklist

- [ ] Dialogue language matches the project.
- [ ] Word count is within the range for the shot's `shot_duration` (counted, not guessed).
- [ ] `motion_prompt` is in English; no hand-written lip-sync line.
- [ ] Storyboard payloads carry no `motion_prompt`; every `shot_duration` is 4/6/8.
