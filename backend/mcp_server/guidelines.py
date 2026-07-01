from pathlib import Path

# Single source of truth for the authoring constraints, served verbatim by the
# get_authoring_guidelines tool. Edit authoring_skill.md, not a string here.
AUTHORING_GUIDELINES = (Path(__file__).parent / "authoring_skill.md").read_text(encoding="utf-8")
