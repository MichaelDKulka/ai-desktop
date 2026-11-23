"""Screenshots of the desktop and the action log are readable only by their owner."""
import os
import stat

import pytest
from PIL import Image

import ai_desktop as ad

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture(autouse=True)
def loose_umask():
    old = os.umask(0o002)  # the usual desktop umask: group-writable, world-readable
    yield
    os.umask(old)


def test_store_files_and_folders_are_private(tmp_path):
    path = ad.save_image(Image.new("RGB", (4, 4)), ad.store_dir() / "20261123-101010-000.png", {"x": 0})
    assert mode(path) == 0o600
    assert mode(ad.store_dir()) == 0o700 and mode(ad.home_dir()) == 0o700


def test_log_pointer_and_stop_files_are_private(run, fake_gui):
    assert run("position")[0] == 0
    [(log, _)] = ad.log_files()
    assert mode(log) == 0o600 and mode(ad.log_dir()) == 0o700
    ad.remember_pointer(5, 5)
    assert mode(ad.home_dir() / ad.POINTER_FILE) == 0o600
    assert run("stop")[0] == 0
    assert mode(ad.stop_path()) == 0o600


def test_existing_loose_folders_are_tightened(run, fake_gui, monkeypatch, tmp_path):
    home = tmp_path / ".ai-desktop"  # the default name: the folder is the tool's own
    (home / "logs").mkdir(parents=True)
    os.chmod(home, 0o775)
    os.chmod(home / "logs", 0o775)
    monkeypatch.setenv("AI_DESKTOP_HOME", str(home))
    assert run("position")[0] == 0
    assert mode(home / "logs") == 0o700 and mode(home) == 0o700


def test_explicit_output_file_is_private_but_its_folder_is_left_alone(tmp_path):
    folder = tmp_path / "mine"
    folder.mkdir()
    os.chmod(folder, 0o755)
    path = ad.save_image(Image.new("RGB", (4, 4)), folder / "shot.png", {"x": 0})
    assert mode(path) == 0o600 and mode(folder) == 0o755


def test_a_custom_home_folder_is_not_changed(monkeypatch, tmp_path, run, fake_gui):
    custom = tmp_path / "project"
    custom.mkdir()
    os.chmod(custom, 0o755)
    monkeypatch.setenv("AI_DESKTOP_HOME", str(custom))
    assert run("position")[0] == 0
    assert mode(custom) == 0o755 and mode(ad.log_dir()) == 0o700
