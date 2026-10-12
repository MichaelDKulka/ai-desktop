import json

import pytest
from PIL import Image

import ai_desktop as ad


def test_grid_keeps_size_and_returns_rgb():
    out = ad.draw_grid(Image.new("RGBA", (300, 200), (255, 255, 255, 255)), 100)
    assert out.size == (300, 200)
    assert out.mode == "RGB"


def test_grid_draws_lines_at_spacing():
    out = ad.draw_grid(Image.new("RGB", (300, 200), (255, 255, 255)), 100)
    for x, y in ((100, 150), (200, 150), (150, 100)):  # on a line, away from any label
        r, g, b = out.getpixel((x, y))
        assert r > g and b > g, f"expected a magenta tint at {(x, y)}, got {(r, g, b)}"
    assert out.getpixel((150, 150)) == (255, 255, 255)  # between lines: untouched


def test_tiny_image_has_no_lines_and_no_crash():
    out = ad.draw_grid(Image.new("RGB", (50, 40), (255, 255, 255)), 100)
    assert out.size == (50, 40)
    assert out.getpixel((25, 20)) == (255, 255, 255)


def test_grid_cli_writes_next_to_input(run, sample_png):
    code, out, _ = run("grid", "--input", str(sample_png), "--spacing", "50")
    assert code == 0
    assert out.startswith("OK grid ") and "width=300 height=200 spacing=50" in out
    assert sample_png.with_name("sample_grid.png").exists()


def test_grid_cli_json_flag_after_subcommand(run, sample_png, tmp_path):
    target = tmp_path / "g.png"
    code, out, _ = run("grid", "--input", str(sample_png), "--json", "-o", str(target))
    data = json.loads(out)
    assert code == 0
    assert data["ok"] is True and data["command"] == "grid" and data["width"] == 300
    assert data["path"] == str(target.resolve())


def test_spacing_too_small_is_a_usage_error(run, sample_png):
    with pytest.raises(SystemExit) as info:
        run("grid", "--input", str(sample_png), "--spacing", "5")
    assert info.value.code == 2


def test_missing_input(run, tmp_path):
    code, _, err = run("grid", "--input", str(tmp_path / "nope.png"))
    assert code == 1
    assert err.startswith("ERROR input image not found")
