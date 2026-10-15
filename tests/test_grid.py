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


def magenta(pixel):
    r, g, b = pixel
    return r > g and b > g


def dark(pixel):
    return max(pixel) < 120


# --- geometry helpers --------------------------------------------------------------------


def test_grid_lines_sit_on_global_multiples():
    assert ad.grid_lines(300, 0, 100) == [(100, 100), (200, 200)]  # local 0 is left out
    assert ad.grid_lines(300, 50, 100) == [(50, 100), (150, 200), (250, 300)]
    assert ad.grid_lines(250, -1920, 100)[:3] == [(20, -1900), (120, -1800), (220, -1700)]
    assert ad.grid_lines(400, 1250, 20, pixel_scale=2) == [(i * 40 - 20, 1250 + i * 20 - 10) for i in range(1, 11)]


def test_font_follows_the_saved_long_edge():
    assert ad.grid_font_px(1366) == 16  # never below LABEL_VIEW_PX
    assert ad.grid_font_px(1920) == 20
    assert round(ad.grid_font_px(1920) * 3286 / 1920) == 34  # a dual-monitor union, drawn before shrinking
    assert round(ad.grid_font_px(1920) * 2) == 40  # 3840 wide, shrunk by half


def test_view_scale():
    assert ad.view_scale(4000, 1920) == 0.48
    assert ad.view_scale(1366, 1920) == 1.0
    assert ad.view_scale(4000, 0) == 1.0 and ad.view_scale(4000, None) == 1.0


@pytest.mark.parametrize(
    "long_edge,box_w,k,scale,expected",
    [
        (64, 40, 1, 1, 100),
        (1366, 44, 1, 1, 100),
        (1920, 52, 1, 1, 100),
        (2560, 70, 1.33, 1, 200),
        (3286, 99, 1.71, 1, 200),
        (3840, 110, 2, 1, 200),
        (5120, 130, 2.67, 1, 250),
        (12000, 130, 1, 1, 500),  # nothing fits: the coarsest candidate
    ],
)
def test_auto_spacing(long_edge, box_w, k, scale, expected):
    assert ad.auto_spacing(long_edge, box_w, k, scale) == expected


def test_auto_spacing_in_a_zoom():
    assert ad.auto_spacing(400, 50, 1, 3, ad.ZOOM_SPACINGS) == 20
    assert ad.auto_spacing(400, 30, 1, 1, ad.ZOOM_SPACINGS) == 50  # 30 px labels need 38 px


def test_label_every():
    assert ad.label_every(44, 1, 100) == 1
    assert ad.label_every(44, 1, 20) == 3
    assert ad.label_every(44, 1, 20, pixel_scale=3) == 1


# --- drawing -----------------------------------------------------------------------------


def test_lines_have_an_outline_that_keeps_their_tint():
    out = ad.draw_grid(Image.new("RGB", (300, 200), (255, 255, 255)), 100)
    # y=130 is away from labels and from the ticks at y=150
    assert magenta(out.getpixel((100, 130))) and magenta(out.getpixel((99, 130))) and magenta(out.getpixel((101, 130)))
    assert out.getpixel((97, 130)) == (255, 255, 255)
    black = ad.draw_grid(Image.new("RGB", (300, 200), (0, 0, 0)), 100)
    assert magenta(black.getpixel((100, 130))) and black.getpixel((100, 130))[0] > 120


def test_ticks_mark_half_spacing_on_the_lines_only():
    out = ad.draw_grid(Image.new("RGB", (300, 200), (255, 255, 255)), 100)
    assert magenta(out.getpixel((150, 100)))  # tick across the y=100 line at x=150
    assert out.getpixel((150, 125)) == (255, 255, 255)
    assert out.getpixel((150, 150)) == (255, 255, 255)  # cell centres stay clear
    plain = ad.draw_grid(Image.new("RGB", (300, 200), (255, 255, 255)), 100, ticks=False)
    assert not magenta(plain.getpixel((150, 103)))


def test_labels_on_all_four_edges():
    out = ad.draw_grid(Image.new("RGB", (1000, 600), (255, 255, 255)), 100)
    near = lambda xs, ys: any(dark(out.getpixel((x, y))) for x in xs for y in ys)  # noqa: E731
    assert near(range(490, 510), range(2, 12))  # x label 500, top
    assert near(range(490, 510), range(588, 598))  # x label 500, bottom
    assert near(range(2, 12), range(295, 305))  # y label 300, left
    assert near(range(988, 998), range(295, 305))  # y label 300, right


def test_landmarks_label_interior_crossings():
    out = ad.draw_grid(Image.new("RGB", (1000, 1000), (255, 255, 255)), 100)
    assert magenta(out.getpixel((400, 400)))  # the crossing itself stays a line pixel
    assert dark(out.getpixel((406, 406)))  # the "400,400" box beside it
    assert not dark(out.getpixel((306, 306)))  # not every crossing
    bare = ad.draw_grid(Image.new("RGB", (1000, 1000), (255, 255, 255)), 100, landmarks=False)
    assert not dark(bare.getpixel((406, 406)))


def test_label_scale_thickens_lines_for_shrinking():
    out = ad.draw_grid(Image.new("RGB", (600, 400), (255, 255, 255)), 100, label_scale=2)
    assert magenta(out.getpixel((100, 230))) and magenta(out.getpixel((98, 230)))
    assert out.getpixel((95, 230)) == (255, 255, 255)


def test_fine_spacing_labels_the_same_values_whatever_the_origin(monkeypatch):
    drawn = []
    real = ad._draw_label

    def spy(draw, text, font, left, top, pad=ad.LABEL_PAD):
        if top == 1:
            drawn.append(text)
        real(draw, text, font, left, top, pad)

    monkeypatch.setattr(ad, "_draw_label", spy)
    for origin in (0, 37, -1950):
        drawn.clear()
        ad.draw_grid(Image.new("RGB", (800, 300), (255, 255, 255)), 20, (origin, 0))
        every = ad.label_every(ad.label_metrics(ad.grid_font((800, 300)), (origin, 0), (800, 300), 2)[0], 1, 20)
        assert drawn and all(int(t) % (20 * every) == 0 for t in drawn), (origin, drawn)


# --- the screenshot pipeline -------------------------------------------------------------


@pytest.fixture
def screen(monkeypatch):
    """A fake 1920x1080 capture with a red dot at (500, 400); returns the capture calls."""
    img = Image.new("RGB", (1920, 1080), (255, 255, 255))
    img.paste(Image.new("RGB", (6, 6), (255, 0, 0)), (497, 397))
    calls = []

    def capture(backend, monitor="all", cursor=False):
        calls.append({"monitor": monitor, "cursor": cursor})
        return ad.Capture(img.copy(), 0, 0, 1.0, "all")

    monkeypatch.setattr(ad, "capture_screen", capture)
    return calls


def test_screenshot_writes_a_sidecar_with_metadata(run, fake_gui, screen):
    code, out, _ = run("screenshot", "--grid")
    assert code == 0 and " spacing=100 " in out and " image_scale=1.0 " in out
    path = ad.store_files()[0][0]
    raw = ad.sidecar_path(path)
    assert raw.name == path.stem + ".raw.png" and raw.exists()
    assert ad.store_files() == [(path, ad.store_files()[0][1])]  # the sidecar stays out of listings
    meta, raw_meta = ad.read_image_meta(path), ad.read_image_meta(raw)
    assert meta["x"] == 0 and meta["spacing"] == 100 and meta["raw"] == raw.name
    assert raw_meta["kind"] == "raw" and raw_meta["width"] == 1920
    with Image.open(raw) as img:
        assert img.getpixel((150, 150)) == (255, 255, 255)  # un-gridded
    with Image.open(path) as img:
        assert magenta(img.getpixel((100, 150)))


def test_zoom_crops_enlarges_and_labels_globally(run, fake_gui, screen):
    code, out, _ = run("screenshot", "--zoom", "500,400", "--radius", "100", "--factor", "2")
    assert code == 0
    assert "width=400 height=400 x=400 y=300 " in out and " zoom=500,400 radius=100 factor=2" in out
    spacing = int(out.split(" spacing=")[1].split()[0])
    assert spacing in ad.ZOOM_SPACINGS
    path = ad.store_files()[0][0]
    assert path.name.endswith(f"-zoom-grid{spacing}.png") and ad.STORE_NAME_RE.match(path.name)
    with Image.open(path) as img:
        r, g, b = img.getpixel((196, 196))  # the red dot, now at the centre (off the lines)
        assert r > 200 and g < 120 and b < 120
        assert magenta(img.getpixel(((500 - 400) * 2, 30)))  # global x 500 sits at local 200
    with Image.open(ad.sidecar_path(path)) as raw:
        assert raw.size == (1920, 1080)  # the full capture, for --seen anywhere


def test_zoom_outside_every_monitor_fails(run, fake_gui, screen):
    code, _, err = run("screenshot", "--zoom", "5000,5000")
    assert code == 1 and "on no monitor" in err
    with pytest.raises(SystemExit) as info:
        run("screenshot", "--zoom", "5000")
    assert info.value.code == 2


def test_zoom_uses_physical_pixels_on_hidpi(monkeypatch):
    raw = Image.new("RGB", (40, 40), (0, 0, 0))
    raw.paste(Image.new("RGB", (1, 1), (255, 255, 255)), (21, 21))
    cap = ad.Capture(raw.resize((20, 20)), 0, 0, 2.0, "all", "none", raw)
    zoomed = ad.zoom_crop(cap, (10, 10, 12, 12), 2)
    # (10, 10) to (12, 12) is physical (20, 20) to (24, 24): the lit pixel (21, 21) shows
    assert zoomed.size == (4, 4) and zoomed.getpixel((1, 1)) == (255, 255, 255)


def test_max_width_shrinks_and_keeps_coordinates(run, fake_gui, monkeypatch):
    def capture(backend, monitor="all", cursor=False):
        return ad.Capture(Image.new("RGB", (4000, 1000), (255, 255, 255)), -2000, 0, 1.0, "all")

    monkeypatch.setattr(ad, "capture_screen", capture)
    code, out, _ = run("screenshot", "--grid")
    assert code == 0 and "width=1920 height=480 x=-2000 y=0 " in out and " image_scale=0.48 " in out
    path = ad.store_files()[0][0]
    with Image.open(ad.sidecar_path(path)) as raw:
        assert raw.size == (4000, 1000)
    with Image.open(path) as img:
        assert magenta(img.getpixel((960, 240)))  # global x 0 at 2000 px, 960 after shrinking
    monkeypatch.setenv("AI_DESKTOP_MAX_WIDTH", "0")
    _, out, _ = run("screenshot", "--grid")
    assert "width=4000 height=1000 " in out
    monkeypatch.setenv("AI_DESKTOP_MAX_WIDTH", "wide")
    code, _, err = run("screenshot")
    assert code == 1 and "AI_DESKTOP_MAX_WIDTH" in err


def test_output_may_not_take_the_sidecar_name(run, fake_gui, screen, tmp_path):
    code, _, err = run("screenshot", "-o", str(tmp_path / "x.raw.png"))
    assert code == 1 and ".raw.png" in err


def test_regrid_a_screenshot_uses_its_raw_copy_and_origin(run, fake_gui, monkeypatch):
    def capture(backend, monitor="all", cursor=False):
        return ad.Capture(Image.new("RGB", (400, 300), (255, 255, 255)), -1920, 0, 1.0, "1")

    monkeypatch.setattr(ad, "capture_screen", capture)
    run("screenshot", "--grid", "--spacing", "30", "--cursor", "none")
    path = ad.store_files()[0][0]
    with Image.open(path) as first:
        assert magenta(first.getpixel((30, 135)))  # global -1890, a multiple of 30
    code, out, _ = run("grid", "--input", str(path), "--spacing", "50")
    assert code == 0 and " x=-1920 y=0 " in out
    with Image.open(path.with_name(f"{path.stem}_grid.png")) as img:
        assert magenta(img.getpixel((70, 135)))  # global -1850 is a multiple of 50
        assert img.getpixel((30, 135)) == (255, 255, 255)  # the first grid is gone


# --- regressions from review -------------------------------------------------------------


def test_zoom_is_never_drawn_bigger_than_it_is_saved(run, fake_gui, screen, monkeypatch):
    sizes = []
    real = ad.draw_grid

    def spy(img, *args, **kwargs):
        sizes.append(img.size)
        return real(img, *args, **kwargs)

    monkeypatch.setattr(ad, "draw_grid", spy)
    code, out, _ = run("screenshot", "--zoom", "960,540", "--radius", "2000", "--factor", "8")
    assert code == 0 and max(sizes[-1]) <= 1920 and "width=1920 height=1080 " in out
    assert " image_scale=1.0 " in out  # 8x asked, but the whole monitor fits only at 1x
    monkeypatch.setenv("AI_DESKTOP_MAX_WIDTH", "0")
    run("screenshot", "--zoom", "960,540", "--radius", "2000", "--factor", "8")
    assert max(sizes[-1]) <= ad.ZOOM_MAX_EDGE


def test_tiny_max_width_is_refused(run, fake_gui, screen, monkeypatch):
    with pytest.raises(SystemExit):
        run("screenshot", "--max-width", "1")
    monkeypatch.setenv("AI_DESKTOP_MAX_WIDTH", "50")
    code, _, err = run("screenshot")
    assert code == 1 and "at least 200" in err


def test_regrid_a_raw_copy_or_a_lone_screenshot_keeps_global_labels(run, fake_gui, monkeypatch, tmp_path):
    def capture(backend, monitor="all", cursor=False):
        return ad.Capture(Image.new("RGB", (400, 300), (255, 255, 255)), -1920, 0, 1.0, "1")

    monkeypatch.setattr(ad, "capture_screen", capture)
    run("screenshot", "--grid", "--cursor", "none")
    path = ad.store_files()[0][0]
    code, out, _ = run("grid", "--input", str(ad.sidecar_path(path)))
    assert code == 0 and " x=-1920 y=0 " in out
    lone = tmp_path / "copy.png"
    lone.write_bytes(path.read_bytes())  # copied without its .raw.png
    code, out, _ = run("grid", "--input", str(lone))
    assert code == 0 and " x=-1920 y=0 " in out


def test_regrid_a_zoom_stays_a_zoom(run, fake_gui, screen):
    _, out, _ = run("screenshot", "--zoom", "500,400", "--radius", "100", "--factor", "2")
    path = ad.store_files()[0][0]
    code, out, _ = run("grid", "--input", str(path), "--spacing", "10")
    assert code == 0 and "width=400 height=400 spacing=10 x=400 y=300 image_scale=2.0" in out
    with Image.open(path.with_name(f"{path.stem}_grid.png")) as img:
        r, g, b = img.getpixel((196, 196))  # still the red dot at the centre
        assert r > 200 and g < 120


def test_output_does_not_clobber_someone_elses_raw_png(run, fake_gui, screen, tmp_path):
    (tmp_path / "photo.raw.png").write_bytes(b"not ours")
    code, _, err = run("screenshot", "-o", str(tmp_path / "photo.png"))
    assert code == 1 and "is not an ai-desktop copy" in err
    assert (tmp_path / "photo.raw.png").read_bytes() == b"not ours"
    assert run("screenshot", "-o", str(tmp_path / "mine.png"))[0] == 0
    assert run("screenshot", "-o", str(tmp_path / "mine.png"))[0] == 0  # our own copy: replaced


def test_seen_refuses_a_raw_copy_of_another_screenshot(run, fake_gui, screen, tmp_path):
    run("screenshot", "-o", str(tmp_path / "a.png"))
    run("screenshot", "-o", str(tmp_path / "a.jpg"))  # same stem: a.raw.png now belongs to a.jpg
    code, _, err = run("click", "--x", "5", "--y", "5", "--seen", str(tmp_path / "a.png"))
    assert code == 1 and "belongs to a.jpg" in err


def test_the_size_prune_never_deletes_the_new_screenshot(run, fake_gui, screen, monkeypatch):
    monkeypatch.setenv("AI_DESKTOP_KEEP_MB", "0.001")
    code, out, _ = run("screenshot", "--grid")
    path = ad.Path(out.split("path=")[1].split()[0])
    assert code == 0 and path.exists() and ad.sidecar_path(path).exists()
