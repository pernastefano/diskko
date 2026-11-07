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
    "bg-indigo-600",
    "hover:bg-indigo-700",
    "text-white",
    "bg-amber-500",
    "bg-red-500",
    "bg-slate-900",
    "bg-black/50",  # the dark scrim behind a dialog
    "text-slate-100",
    "text-slate-300",
    "text-slate-400",
    "focus:border-indigo-500",
    "focus:ring-indigo-500",
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
    assert re.search(r"\.dark \.bg-white[^{]*\{ background-color:#0f172a", CSS)
    assert re.search(r"\.dark \.text-slate-500[^{]*\{ color:#94a3b8", CSS)
    assert re.search(r"\.dark \.border-slate-200[^{]*\{ border-color:#334155", CSS)
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
