from __future__ import annotations

import json

from jellyfin_ass2pgs import cache


def test_normal_work_directory_is_removed_on_close(tmp_path) -> None:
    lease = cache.prepare_work_directory(tmp_path, prefix="media-", keep_temp=False)
    path = lease.path

    assert path.exists()
    assert (path / cache.WORK_MARKER_NAME).exists()

    lease.close()

    assert not path.exists()


def test_keep_temp_work_directory_survives_close_and_future_cleanup(tmp_path) -> None:
    kept = cache.prepare_work_directory(tmp_path, prefix="kept-", keep_temp=True)
    kept_path = kept.path
    kept.close()

    normal = cache.prepare_work_directory(tmp_path, prefix="normal-", keep_temp=False)
    normal.close()

    assert kept_path.exists()
    assert kept_path not in normal.cleanup_report.removed


def test_stale_marked_directory_is_removed(tmp_path, monkeypatch) -> None:
    stale = tmp_path / "media-stale"
    stale.mkdir()
    (stale / cache.WORK_MARKER_NAME).write_text(
        json.dumps({"pid": 123456, "created_at": 0, "keep_temp": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cache, "_pid_is_alive", lambda pid: False)

    report = cache.cleanup_orphaned_work_dirs(tmp_path)

    assert report.removed == (stale,)
    assert not stale.exists()


def test_unmanaged_directory_is_reported_but_not_removed(tmp_path) -> None:
    unmanaged = tmp_path / "old-debug-output"
    unmanaged.mkdir()

    report = cache.cleanup_orphaned_work_dirs(tmp_path)

    assert unmanaged.exists()
    assert len(report.warnings) == 1
    assert str(unmanaged) in report.warnings[0]
