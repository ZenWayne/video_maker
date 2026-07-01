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
- **Lip-sync:** call `update_motion` with `sync_lip_marker=true` (default) and the
  lip-sync line — `The character says: "..."` — is appended and kept in sync with
  the current dialogue automatically. Do NOT hand-write that line yourself.

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

## Pre-submit checklist

- [ ] Dialogue language matches the project.
- [ ] Word count is within the range for the shot's `shot_duration` (counted, not guessed).
- [ ] `motion_prompt` is in English; no hand-written lip-sync line.
- [ ] Storyboard payloads carry no `motion_prompt`; every `shot_duration` is 4/6/8.
