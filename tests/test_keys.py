import pytest

import ai_desktop as ad


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Return", "enter"),
        ("ESC", "esc"),
        ("escape", "esc"),
        ("control", "ctrl"),
        ("cmd", "meta"),
        ("command", "meta"),
        ("win", "meta"),
        ("super", "meta"),
        ("option", "alt"),
        ("pgdn", "pagedown"),
        ("del", "delete"),
        ("enter", "enter"),
    ],
)
def test_aliases(name, expected):
    assert ad.normalize_key(name) == expected


def test_single_character_is_kept_verbatim():
    assert ad.normalize_key("A") == "A"
    assert ad.normalize_key("+") == "+"


def test_empty_name_is_rejected():
    with pytest.raises(ad.CliError):
        ad.normalize_key("   ")


def test_us_charmap_covers_every_typeable_character():
    assert set(ad.US_CHARMAP) == ad.TYPEABLE


@pytest.mark.parametrize(
    "char,code,shift",
    [
        ("a", 30, False),
        ("A", 30, True),
        ("1", 2, False),
        ("!", 2, True),
        ("0", 11, False),
        (")", 11, True),
        ("/", 53, False),
        ("?", 53, True),
        ("\\", 43, False),
        ("|", 43, True),
        ("=", 13, False),
        ("+", 13, True),
        (" ", 57, False),
        ("\n", 28, False),
        ("\t", 15, False),
    ],
)
def test_us_charmap_codes(char, code, shift):
    assert ad.US_CHARMAP[char] == (code, shift)


def test_named_key_codes():
    assert ad.UINPUT_KEYS["enter"] == 28
    assert ad.UINPUT_KEYS["f1"] == 59
    assert ad.UINPUT_KEYS["f10"] == 68
    assert ad.UINPUT_KEYS["f12"] == 88
    assert ad.UINPUT_KEYS["meta"] == 125


def test_uinput_resolve_key():
    backend = ad.UInputBackend.__new__(ad.UInputBackend)  # skip the Linux-only __init__
    assert backend.resolve_key("Return") == (28, False)
    assert backend.resolve_key("CMD") == (125, False)
    assert backend.resolve_key("A") == (30, True)
    with pytest.raises(ad.CliError, match="unknown key"):
        backend.resolve_key("bogus")
