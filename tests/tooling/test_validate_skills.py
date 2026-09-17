from __future__ import annotations

from typing import TYPE_CHECKING

from tests.tooling.conftest import load_script

if TYPE_CHECKING:
    from pathlib import Path

VALID = """---
name: {name}
description: Use when testing the validator. Validates demo skills in third person.
metadata:
  version: "1.0.0"
---

# Demo

Run `scripts/tool.py`.
"""


def write_skill(root: Path, dirname: str, name: str | None = None, text: str | None = None) -> Path:
    skill = root / dirname
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "tool.py").write_text("print(1)\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(text or VALID.format(name=name or dirname), encoding="utf-8")
    return skill


def test_valid_skill_has_no_errors(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    assert validate.validate_skill(write_skill(tmp_path, "demo-skill")) == []


def test_parse_frontmatter_reads_nested_metadata(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    meta, body = validate.parse_frontmatter(VALID.format(name="demo-skill"))
    assert meta["name"] == "demo-skill"
    assert meta["metadata"] == {"version": "1.0.0"}
    assert body.lstrip().startswith("# Demo")


def test_parse_frontmatter_folds_block_scalars_and_sequences(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    text = (
        "---\n"
        "name: demo-skill\n"
        "description: >-\n"
        "  Use when testing the validator.\n"
        "  Second line of the folded scalar.\n"
        "allowed-tools:\n"
        "  - WebFetch(domain:example.com)\n"
        "  - Bash(curl *example.com/*)\n"
        "license: |\n"
        "  first\n"
        "  second\n"
        "---\n\n# Demo\n\nRun `scripts/tool.py`.\n"
    )
    meta, body = validate.parse_frontmatter(text)
    assert meta["description"] == (
        "Use when testing the validator. Second line of the folded scalar."
    )
    assert meta["allowed-tools"] == ["WebFetch(domain:example.com)", "Bash(curl *example.com/*)"]
    assert meta["license"] == "first\nsecond"
    assert body.lstrip().startswith("# Demo")
    assert validate.validate_skill(write_skill(tmp_path, "demo-skill", text=text)) == []


def test_a_reference_that_ends_a_sentence_keeps_its_real_filename():
    """`see references/guide.md.` names guide.md, not `guide.md.`.

    Asserted on the pattern rather than through `validate_skill`, because a filesystem check
    cannot see this on Windows: the Win32 API strips a path's trailing dots, so
    `Path("references/guide.md.").exists()` is True there and False on Linux — which is exactly
    how this reached a green local gate and a red CI.
    """
    validate = load_script("scripts/validate_skills.py")
    body = "See references/guide.md. Then scripts/run.py, and assets/logo.png."

    assert validate.REFERENCE_RE.findall(body) == [
        "references/guide.md",
        "scripts/run.py",
        "assets/logo.png",
    ]


def test_vendored_langfuse_skill_is_valid(repo_root):
    validate = load_script("scripts/validate_skills.py")
    assert validate.validate_skill(repo_root / ".agents" / "skills" / "langfuse") == []


def test_name_must_match_directory(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    errors = validate.validate_skill(write_skill(tmp_path, "demo-skill", name="other-name"))
    assert any("directory" in e for e in errors)


def test_rejects_bad_names_and_missing_description(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    bad = "---\nname: Bad--Name\n---\n# x\n"
    errors = validate.validate_skill(write_skill(tmp_path, "bad--name", text=bad))
    assert any("name" in e.lower() for e in errors)
    assert any("description" in e for e in errors)


def test_rejects_reserved_words_unknown_keys_and_missing_references(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    text = (
        "---\nname: demo-skill\ndescription: Something.\ncontext: fork\n---\n"
        "# Demo\nSee `references/missing.md` and `scripts\\\\win.py`.\n"
    )
    errors = validate.validate_skill(write_skill(tmp_path, "demo-skill", text=text))
    joined = "\n".join(errors)
    assert "context" in joined
    assert "references/missing.md" in joined
    assert "backslash" in joined.lower()
    text2 = "---\nname: claude-helper\ndescription: Something.\n---\n# x\n"
    errors2 = validate.validate_skill(write_skill(tmp_path, "claude-helper", text=text2))
    assert any("reserved" in e for e in errors2)


def test_body_length_limit(tmp_path):
    validate = load_script("scripts/validate_skills.py")
    long_body = VALID.format(name="demo-skill") + ("line\n" * 520)
    errors = validate.validate_skill(write_skill(tmp_path, "demo-skill", text=long_body))
    assert any("500" in e for e in errors)


def test_main_reports_all_skills(tmp_path, capsys):
    validate = load_script("scripts/validate_skills.py")
    write_skill(tmp_path, "demo-skill")
    write_skill(tmp_path, "other-skill", name="mismatch")
    assert validate.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "other-skill" in out
    assert "1 skill(s) valid" in out
