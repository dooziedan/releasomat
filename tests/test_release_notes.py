"""Release notes for the GitHub Releases page, cut from CHANGELOG.md (scripts/release_notes.py)."""

from scripts import release_notes

CHANGELOG = """# Changelog

## Unreleased

### Added
- Something not released yet.

## [0.10.0] - 2026-12-07

### Added
- The ten.

## 0.1.0 - 2026-11-01

### Fixed
- The first fix.
"""


def test_a_versions_section_without_its_heading_or_the_next_one():
    assert release_notes.section(CHANGELOG, "0.10.0") == "### Added\n- The ten."
    assert release_notes.section(CHANGELOG, "0.1.0") == "### Fixed\n- The first fix."


def test_a_missing_version_is_none():
    assert release_notes.section(CHANGELOG, "0.2.0") is None
    assert release_notes.section(CHANGELOG, "0.1") is None  # not the start of 0.1.0
    assert release_notes.notes(CHANGELOG, "0.2.0") is None


def test_notes_say_which_image_to_pull():
    text = release_notes.notes(CHANGELOG, "0.10.0")
    assert text.startswith("### Added\n- The ten.")
    assert "`ghcr.io/dooziedan/releasomat:0.10.0`" in text


def test_the_command_fails_without_a_section(monkeypatch, tmp_path, capsys):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(release_notes, "CHANGELOG", changelog)
    assert release_notes.main(["v0.10.0"]) == 0
    assert "The ten." in capsys.readouterr().out
    assert release_notes.main(["v0.2.0"]) == 1
    assert "no section '## [0.2.0]'" in capsys.readouterr().err
    assert release_notes.main([]) == 2
