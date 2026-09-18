"""Tests for crash-safe and permission-aware file writes."""

import os
import stat

import pytest

from living_ink import safeio


def mode_of(path):
    """Return the permission bits of a path."""
    return stat.S_IMODE(path.stat().st_mode)


class TestWriteTextAtomic:
    """The write either lands completely or not at all."""

    def test_writes_the_content(self, tmp_path):
        target = tmp_path / "note.md"
        safeio.write_text_atomic(target, "hello")
        assert target.read_text() == "hello"

    def test_creates_missing_parents(self, tmp_path):
        target = tmp_path / "a" / "b" / "note.md"
        safeio.write_text_atomic(target, "hello")
        assert target.read_text() == "hello"

    def test_overwrites_an_existing_file(self, tmp_path):
        target = tmp_path / "note.md"
        target.write_text("old")
        safeio.write_text_atomic(target, "new")
        assert target.read_text() == "new"

    def test_leaves_no_temporary_file_behind(self, tmp_path):
        target = tmp_path / "note.md"
        safeio.write_text_atomic(target, "hello")
        assert list(tmp_path.iterdir()) == [target]

    def test_a_failed_write_leaves_the_original_intact(self, tmp_path, monkeypatch):
        target = tmp_path / "note.md"
        target.write_text("original")

        def explode(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(safeio.os, "replace", explode)
        with pytest.raises(OSError):
            safeio.write_text_atomic(target, "replacement")

        assert target.read_text() == "original"
        assert list(tmp_path.iterdir()) == [target]

    def test_an_interrupted_write_leaves_the_original_intact(self, tmp_path, monkeypatch):
        target = tmp_path / "note.md"
        target.write_text("original")

        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(safeio.os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            safeio.write_text_atomic(target, "replacement")

        assert target.read_text() == "original"
        assert list(tmp_path.iterdir()) == [target]

    def test_applies_the_requested_mode(self, tmp_path):
        target = tmp_path / "note.md"
        safeio.write_text_atomic(target, "hello", mode=0o640)
        assert mode_of(target) == 0o640

    def test_round_trips_unicode(self, tmp_path):
        target = tmp_path / "note.md"
        safeio.write_text_atomic(target, "Grüße — 日本語")
        assert target.read_text(encoding="utf-8") == "Grüße — 日本語"


class TestWriteSecretAtomic:
    """Credentials are never readable by anyone but their owner."""

    def test_the_file_is_owner_only(self, tmp_path):
        target = tmp_path / "config.yml"
        safeio.write_secret_atomic(target, "token: abc")
        assert mode_of(target) == 0o600

    def test_the_directory_is_owner_only(self, tmp_path):
        target = tmp_path / "living-ink" / "config.yml"
        safeio.write_secret_atomic(target, "token: abc")
        assert mode_of(target.parent) == 0o700

    def test_an_existing_loose_file_is_tightened(self, tmp_path):
        target = tmp_path / "config.yml"
        target.write_text("old")
        target.chmod(0o666)
        safeio.write_secret_atomic(target, "token: abc")
        assert mode_of(target) == 0o600

    def test_the_secret_is_never_written_at_a_loose_mode(self, tmp_path):
        """The temporary file must already be 0600 before the bytes land."""
        target = tmp_path / "config.yml"
        observed = {}
        real_open = safeio.os.open

        def record(path, flags, mode=0o777, **kwargs):
            if str(path).endswith(".tmp"):
                observed["mode"] = mode
            return real_open(path, flags, mode, **kwargs)

        safeio.os.open = record
        try:
            safeio.write_secret_atomic(target, "token: abc")
        finally:
            safeio.os.open = real_open

        assert observed["mode"] == 0o600


class TestRestrictPermissions:
    """Loose permissions on an existing file are repaired in place."""

    def test_tightens_a_world_readable_file(self, tmp_path):
        target = tmp_path / "config.yml"
        target.write_text("token: abc")
        target.chmod(0o644)
        assert safeio.restrict_permissions(target) is True
        assert mode_of(target) == 0o600

    def test_preserves_owner_execute(self, tmp_path):
        target = tmp_path / "wrapper.sh"
        target.write_text("#!/bin/sh")
        target.chmod(0o755)
        safeio.restrict_permissions(target)
        assert mode_of(target) == 0o700

    def test_reports_no_change_when_already_restricted(self, tmp_path):
        target = tmp_path / "config.yml"
        target.write_text("token: abc")
        target.chmod(0o600)
        assert safeio.restrict_permissions(target) is False
        assert mode_of(target) == 0o600

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert safeio.restrict_permissions(tmp_path / "absent.yml") is False


class TestDirectorySync:
    """Directory flushing is best-effort and never fails a completed write."""

    def test_write_succeeds_when_directory_fsync_is_unsupported(self, tmp_path, monkeypatch):
        target = tmp_path / "note.md"
        real_fsync = os.fsync

        def fail_on_directories(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("not supported")
            return real_fsync(fd)

        monkeypatch.setattr(safeio.os, "fsync", fail_on_directories)
        safeio.write_text_atomic(target, "hello")
        assert target.read_text() == "hello"
