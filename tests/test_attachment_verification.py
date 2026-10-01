from pathlib import Path

import pytest

from app.verify_attachments import verify_attachment_tree


def test_attachment_tree_matches_database_metadata(tmp_path: Path):
    payload = tmp_path / "evidence.pdf"
    payload.write_bytes(b"verified evidence")

    verify_attachment_tree(
        tmp_path,
        [("evidence.pdf", "e67c6d223f7cc6495f0c65e9adb1aefc235742969c4f537449b2583c2fc71f14")],
    )


@pytest.mark.parametrize("failure", ["missing", "altered", "orphan", "duplicate"])
def test_attachment_tree_rejects_inconsistent_sets(tmp_path: Path, failure: str):
    payload = tmp_path / "evidence.pdf"
    payload.write_bytes(b"verified evidence")
    expected = [("evidence.pdf", "e67c6d223f7cc6495f0c65e9adb1aefc235742969c4f537449b2583c2fc71f14")]

    if failure == "missing":
        payload.unlink()
    elif failure == "altered":
        payload.write_bytes(b"altered")
    elif failure == "orphan":
        (tmp_path / "orphan.txt").write_text("orphan")
    elif failure == "duplicate":
        expected.append(expected[0])

    with pytest.raises(ValueError):
        verify_attachment_tree(tmp_path, expected)
