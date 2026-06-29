AUTHORING_GUIDELINES = """\
Dialogue (text / 台词):
- Write in the SAME language as the project's existing dialogue / theme.
- Word-count targets by shot_duration (English-word approximation; advisory, not blocking):
  4s → 8-10 words, 6s → 13-16 words, 8s → 18-21 words.
- Keep it natural, in the character's voice (personality is implied by the reference images).

Motion (motion_prompt / 动作):
- Write the motion prompt in ENGLISH.
- Describe camera movement and talking-head physiological cues; preserve visual fidelity.
- If the shot has dialogue, the lip-sync marker (The character says: \"...\") is kept in sync
  automatically when you use update_motion with sync_lip_marker=true.

Storyboard:
- Storyboard JSON shots carry structure + dialogue only (no motion_prompt).
- Set motion_prompt afterward via update_motion / batch_update_shots.
- replace_storyboard requires the project to be in script_review status.
"""
