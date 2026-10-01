from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .config import env
from typing import Iterable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.main import database_url_from_env
from app.models import Attachment


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_attachment_tree(root: Path, expected_rows: Iterable[tuple[str, str]]) -> None:
    expected: dict[str, str] = {}
    for stored_filename, digest in expected_rows:
        if (
            not stored_filename
            or Path(stored_filename).name != stored_filename
            or stored_filename in {".", ".."}
            or stored_filename in expected
        ):
            raise ValueError("Attachment metadata contains an unsafe or duplicate stored filename.")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
            raise ValueError(f"Attachment metadata contains an invalid digest for {stored_filename}.")
        expected[stored_filename] = digest.lower()

    if not root.is_dir() or root.is_symlink():
        raise ValueError("Attachment storage is missing or is not a real directory.")

    actual: dict[str, Path] = {}
    for child in root.iterdir():
        if child.is_symlink() or not child.is_file():
            raise ValueError(f"Unexpected attachment storage member: {child.name}.")
        actual[child.name] = child

    missing = sorted(set(expected) - set(actual))
    orphaned = sorted(set(actual) - set(expected))
    if missing or orphaned:
        raise ValueError(f"Attachment set mismatch; missing={missing}, orphaned={orphaned}.")

    for stored_filename, expected_digest in expected.items():
        if file_sha256(actual[stored_filename]) != expected_digest:
            raise ValueError(f"Attachment digest mismatch: {stored_filename}.")


def main() -> None:
    attachment_dir = Path(env("SPM_ATTACHMENT_DIR", "/app/data/attachments"))
    engine = create_engine(database_url_from_env(), pool_pre_ping=True)
    try:
        with Session(engine) as db:
            rows = db.execute(select(Attachment.stored_filename, Attachment.sha256)).all()
        verify_attachment_tree(attachment_dir, rows)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
