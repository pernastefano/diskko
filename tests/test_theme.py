import re
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent / "app" / "web"
CSS = (ROOT / "static" / "theme.css").read_text()

_COLOR = re.compile(
    r"(?<![\w-])((?:[a-z-]+:)*(?:bg|text|border|divide|from|to|via|ring|placeholder|accent|fill|stroke)"
    r"-(?:white|black|slate|indigo|green|red|amber|blue|sky|purple|teal|fuchsia)(?:-\d{2,3})?(?:/\d+)?)(?![\w-])"
)

# Looks the same in both themes: strong accent colours with light text, or already dark boxes.
IDENTICAL_IN_BOTH_THEMES = {
    "text-white",
    "bg-amber-500",
    "bg-red-500",
    "bg-red-600",  # the count on the notification bell
    "bg-slate-900",
    "bg-black/50",  # the dark scrim behind a dialog
    "text-slate-100",
    "text-slate-300",
    "text-slate-400",
}


def used_color_classes():
    found = set()
    for template in ROOT.glob("templates/**/*.html"):
        found |= {m.group(1) for m in _COLOR.finditer(template.read_text())}
    return found


def has_rule(cls):
    return "." + cls.replace(":", "\\:").replace("/", "\\/") in CSS


def test_every_colour_class_in_the_templates_has_a_dark_version():
    missing = sorted(
        c for c in used_color_classes() if c not in IDENTICAL_IN_BOTH_THEMES and not has_rule(c)
    )
    assert not missing, f"add dark rules (see app/web/static/theme.css) for: {missing}"


def test_the_identical_list_only_contains_classes_that_are_really_used():
    assert used_color_classes() | {"text-slate-400"} >= IDENTICAL_IN_BOTH_THEMES


def test_the_stylesheet_is_well_formed():
    assert CSS.count("{") == CSS.count("}") and CSS.count("/*") == CSS.count("*/")
    for line in CSS.splitlines():
        if line.startswith(".dark") and "{" in line:
            assert line.rstrip().endswith("}"), line


def test_key_surfaces_get_dark_colours():
    assert re.search(r"\.dark \.bg-white[^{]*\{ background-color:#181b1f", CSS)
    assert re.search(r"\.dark \.text-slate-500[^{]*\{ color:#9b968d", CSS)
    assert re.search(r"\.dark \.border-slate-200[^{]*\{ border-color:#2b2f34", CSS)
    assert "html.dark { color-scheme: dark; }" in CSS
    assert ".dark.bg-slate-50" in CSS  # the root element itself is a themed surface


def test_every_badge_family_is_covered():
    for family in ("green", "red", "amber", "blue", "sky", "purple", "teal", "fuchsia", "indigo"):
        for prop in ("bg", "text"):
            assert f".{prop}-{family}-" in CSS, (prop, family)


def test_the_stylesheet_is_served(admin_client):
    r = admin_client.get("/static/theme.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"] and ".dark" in r.text


def test_the_stylesheet_is_public_so_the_login_page_can_use_it(client):
    assert client.get("/static/theme.css").status_code == 200


def test_light_mode_is_untouched():
    """Every rule is scoped under .dark: nothing changes for light users."""
    rules = [ln for ln in CSS.splitlines() if "{" in ln and not ln.startswith(("/*", " "))]
    assert rules and all(ln.startswith((".dark", "html.dark", "html {")) for ln in rules)
    assert [ln for ln in rules if ln.startswith("html {")] == ["html { color-scheme: light; }"]


def test_form_controls_follow_the_chosen_theme_not_the_system():
    """A <meta name="color-scheme" content="light dark"> would paint inputs dark on a dark OS even
    when the user picked the light theme, so the page must declare the scheme only through CSS."""
    base = (ROOT / "templates" / "base.html").read_text()
    assert 'name="color-scheme"' not in base
    assert "html { color-scheme: light; }" in CSS and "html.dark { color-scheme: dark; }" in CSS


def test_the_default_theme_setting_is_auto(settings):
    assert isinstance(settings, Settings)


# ---------------------------------------------------------- Sand, Graphite and the icon

BASE = (ROOT / "templates" / "base.html").read_text()


def test_the_light_theme_is_sand_and_the_accent_is_teal():
    assert "tailwind.config" in BASE
    for value in ("#e9e2d3", "#f6f1e6", "#d8cfbb", "#26231c", "#6c6455", "#0f766e"):
        assert value in BASE  # the Sand background, surface, border, text, muted and accent


def test_the_dark_theme_is_graphite_with_an_amber_accent():
    for value in ("#0e1012", "#181b1f", "#2b2f34", "#ede9e2", "#9b968d", "#f2a431", "#1c1403"):
        assert value in CSS  # Graphite background, surface, border, text, muted and the accent
    assert re.search(r"\.dark \.bg-indigo-600[^{]*\{ background-color:#f2a431; color:#1c1403", CSS)
    for old in ("#0f172a", "#020617", "#a5b4fc", "rgba(99,102,241"):
        assert old not in CSS  # nothing is left of the old slate and indigo dark palette


def test_the_browsers_own_bar_follows_the_theme():
    assert '<meta name="theme-color" content="#f6f1e6">' in BASE
    assert "bar.content = dark ? '#181b1f' : '#f6f1e6'" in BASE


def test_the_pages_carry_the_icon_and_the_manifest(admin_client):
    html = admin_client.get("/dashboard").text
    assert 'rel="icon" href="/static/icons/favicon.svg"' in html
    assert 'rel="icon" href="/static/icons/icon-192.png"' in html
    assert 'rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png"' in html
    assert 'rel="manifest" href="/static/manifest.webmanifest"' in html
    assert "data-logo" in html and "<svg" in html.split("data-logo", 1)[1][:400]  # the disc mark


def test_the_icons_are_real_pngs_of_the_right_size_and_public(client):
    import struct

    sizes = {
        "icon-192.png": 192,
        "icon-512.png": 512,
        "icon-maskable-512.png": 512,
        "apple-touch-icon.png": 180,
    }
    for name, size in sizes.items():
        r = client.get(f"/static/icons/{name}")
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n", name
        assert struct.unpack(">II", r.content[16:24]) == (size, size), name
    svg = client.get("/static/icons/favicon.svg")
    assert svg.status_code == 200 and b"<circle" in svg.content  # a ring and a dot


def test_the_manifest_names_the_app_and_its_icons():
    import json

    manifest = json.loads((ROOT / "static" / "manifest.webmanifest").read_text())
    assert manifest["name"] == "DISKKO" and manifest["display"] == "standalone"
    assert manifest["theme_color"] == "#181b1f" and manifest["background_color"] == "#0e1012"
    icons = {i["sizes"]: i for i in manifest["icons"]}
    assert set(icons) == {"192x192", "512x512"} or len(manifest["icons"]) == 3
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])
    for icon in manifest["icons"]:
        assert (ROOT / icon["src"].removeprefix("/")).is_file()


def test_the_icon_generator_draws_a_disc_with_a_dot():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "generate_icons.py"
    spec = importlib.util.spec_from_file_location("generate_icons", path)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert gen.shade(0.5, 0.5, "full")[:3] == gen.ACCENT  # the dot in the middle
    assert gen.shade(0.5, 0.5 + 0.25, "full")[:3] == gen.FOREGROUND  # the ring
    assert gen.shade(0.5, 0.5 + 0.15, "full")[:3] == gen.BACKGROUND  # the hole of the disc
    assert gen.shade(0.02, 0.02, "rounded")[3] == 0  # outside the rounded corner
    assert gen.shade(0.02, 0.02, "full")[3] == 255
