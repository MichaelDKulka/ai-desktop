import json
import os
import time

import pytest
from PIL import Image

import ai_desktop as ad


def make(folder, name, age_days=0, size=10):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_store_dir_honours_env(tmp_path):
    assert ad.store_dir() == tmp_path / "home" / "screenshots"


def test_store_name_pattern():
    good = [
        "20261012-101905-123.png",
        "20261012-101905-123-grid100.png",
        "20261012-101905-123-2-grid50.png",
        "20261012-101905-123-m1-zoom-grid20.png",
        "20261012-101905-123-grid100.raw.png",
    ]
    bad = [
        "foo.png", "20261012-101905-123.jpg", "screenshot.png", "x20261012-101905-123.png", ".last-prune",
        "20261012-101905-123-raw.png", "20261012-101905-123.raw.jpg",
    ]
    assert all(ad.STORE_NAME_RE.match(name) for name in good)
    assert not any(ad.STORE_NAME_RE.match(name) for name in bad)


def test_new_path_shape_and_collision_guard(monkeypatch):
    fixed = ad.datetime(2026, 1, 2, 3, 4, 5, 678000)

    class Fixed(ad.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(ad, "datetime", Fixed)
    first = ad.new_screenshot_path(True, 100)
    assert first.parent == ad.store_dir()
    assert first.name == "20260102-030405-678-grid100.png"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"")
    assert ad.new_screenshot_path(True, 100).name == "20260102-030405-678-1-grid100.png"
    assert ad.new_screenshot_path(False, 100).name == "20260102-030405-678.png"


def test_prune_by_age_only_touches_own_files():
    folder = ad.store_dir()
    old = make(folder, "20260101-000000-000.png", age_days=40)
    new = make(folder, "20261010-000000-000.png", age_days=1)
    stray = make(folder, "foo.png", age_days=40)
    other_ext = make(folder, "20260101-000000-000.txt", age_days=40)
    (folder / "sub").mkdir()
    assert ad.prune_store(30, None) == [old]
    assert not old.exists()
    assert new.exists() and stray.exists() and other_ext.exists() and (folder / "sub").is_dir()


def test_prune_ignores_symlinks(tmp_path):
    folder = ad.store_dir()
    folder.mkdir(parents=True)
    target = make(tmp_path, "target.png", age_days=40)
    link = folder / "20260101-000000-000.png"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available here")
    assert ad.prune_store(30, None) == []
    assert target.exists() and link.is_symlink()


def test_prune_by_size_removes_oldest_first():
    folder = ad.store_dir()
    mb = 1024 * 1024
    files = [make(folder, f"2026010{i}-000000-000.png", age_days=10 - i, size=mb) for i in range(1, 6)]
    removed = ad.prune_store(None, 2.5)
    assert set(removed) == set(files[:3])  # oldest three go, 2 MB stay
    assert all(path.exists() for path in files[3:])


def test_prune_with_both_rules_off_keeps_everything():
    old = make(ad.store_dir(), "20260101-000000-000.png", age_days=400)
    assert ad.prune_store(None, None) == []
    assert old.exists()


def test_keep_setting(monkeypatch):
    assert ad.keep_setting("AI_DESKTOP_KEEP_DAYS", 30) == 30.0
    monkeypatch.setenv("AI_DESKTOP_KEEP_DAYS", "off")
    assert ad.keep_setting("AI_DESKTOP_KEEP_DAYS", 30) is None
    monkeypatch.setenv("AI_DESKTOP_KEEP_DAYS", "0")
    assert ad.keep_setting("AI_DESKTOP_KEEP_DAYS", 30) is None
    monkeypatch.setenv("AI_DESKTOP_KEEP_DAYS", "7.5")
    assert ad.keep_setting("AI_DESKTOP_KEEP_DAYS", 30) == 7.5
    monkeypatch.setenv("AI_DESKTOP_KEEP_DAYS", "abc")
    with pytest.raises(ad.CliError, match="AI_DESKTOP_KEEP_DAYS"):
        ad.keep_setting("AI_DESKTOP_KEEP_DAYS", 30)


def test_prune_if_due_runs_at_most_hourly():
    folder = ad.store_dir()
    first = make(folder, "20260101-000000-000.png", age_days=40)
    assert ad.prune_if_due() == [first]
    second = make(folder, "20260102-000000-000.png", age_days=40)
    assert ad.prune_if_due() is None  # within the hour: skipped
    assert second.exists()
    marker = folder / ad.PRUNE_MARKER
    stamp = time.time() - 2 * ad.PRUNE_INTERVAL
    os.utime(marker, (stamp, stamp))
    assert ad.prune_if_due() == [second]


def fake_capture(backend, monitor="all", **kwargs):
    return ad.Capture(Image.new("RGB", (64, 48)), 0, 0, 1.0, "all")


def test_screenshot_goes_into_the_store(run, fake_gui, monkeypatch):
    monkeypatch.setattr(ad, "capture_screen", fake_capture)
    code, out, _ = run("screenshot", "--grid", "--spacing", "20")
    assert code == 0 and "width=64 height=48 " in out and " spacing=20 " in out and "pruned=0" in out
    files = ad.store_files()
    assert len(files) == 1 and files[0][0].name.endswith("-grid20.png")
    assert (ad.store_dir() / ad.PRUNE_MARKER).exists()


def test_screenshot_explicit_output_is_outside_the_store(run, fake_gui, monkeypatch, tmp_path):
    monkeypatch.setattr(ad, "capture_screen", fake_capture)
    target = tmp_path / "mine.png"
    code, out, _ = run("screenshot", "-o", str(target))
    assert code == 0 and target.exists() and "pruned=skipped" in out
    assert ad.store_files() == []


def test_screenshots_subcommand(run):
    code, _, err = run("screenshots", "latest")
    assert code == 1 and "no screenshots" in err

    folder = ad.store_dir()
    old = make(folder, "20260101-000000-000.png", age_days=40, size=100)
    new = make(folder, "20261010-000000-000.png", age_days=1, size=100)

    code, out, _ = run("screenshots", "latest")
    assert code == 0 and f"path={new}" in out

    code, out, _ = run("screenshots", "list", "--limit", "1")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("OK screenshots action=list") and lines[1:] == [str(new)]

    _, out, _ = run("--json", "screenshots", "list")
    assert [row["path"] for row in json.loads(out)["files"]] == [str(new), str(old)]

    _, out, _ = run("screenshots", "dir")
    assert "count=2" in out and "keep_days=30" in out and "keep_mb=2048" in out

    _, out, _ = run("screenshots", "clean")
    assert "deleted=1 kept=1" in out and not old.exists() and new.exists()

    _, out, _ = run("screenshots", "clean", "--all")
    assert "deleted=1 kept=0" in out and not new.exists()


def test_sidecars_are_hidden_from_listings_but_pruned():
    folder = ad.store_dir()
    image = make(folder, "20260101-000000-000-grid100.png", age_days=40)
    raw = make(folder, "20260101-000000-000-grid100.raw.png", age_days=40)
    assert ad.store_files() == [(image, ad.store_files()[0][1])]
    assert {path for path, _ in ad.store_files(include_raw=True)} == {image, raw}
    assert set(ad.prune_store(30, None)) == {image, raw}
