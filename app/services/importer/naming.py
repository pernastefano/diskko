"""Folder and file names of imported albums, from templates.

A template is plain text with {placeholders}: artist, album, year, track, disc, title. Numbers
take a width like {track:02d}. The folder template may contain "/" (one folder per part); every
part is cleaned so that it is a safe name on any file system a NAS may share (SMB included).
"""

import re
import string
import unicodedata
from dataclasses import dataclass

DEFAULT_FOLDER_TEMPLATE = "{artist}/{album} ({year})"
DEFAULT_FILE_TEMPLATE = "{track:02d} - {title}"
MULTI_DISC_FILE_TEMPLATE = "{disc}-{track:02d} - {title}"  # used when no {disc} is in the template
PLACEHOLDERS = ("artist", "album", "year", "track", "disc", "title")
_NUMERIC = ("year", "track", "disc")
_SPEC = re.compile(r"^0?\d{1,2}d$")
_ILLEGAL = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_EMPTY_BRACKETS = re.compile(r"\s*[(\[]\s*[)\]]")
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
MAX_PART_BYTES = 180  # well under the 255 bytes of most file systems, room for the extension


class TemplateError(ValueError):
    """The template is not usable; `code` is the suffix of an i18n key `settings.error.*`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_template(template: str, *, required: str) -> str:
    """The template, if it only uses known placeholders (and contains `required`)."""
    value = template.strip()
    if not value or len(value) > 200:
        raise TemplateError("template_invalid")
    if value.startswith("/") or ".." in value.replace("{", "").replace("}", ""):
        raise TemplateError("template_invalid")
    used = set()
    try:
        for _, field_name, spec, conversion in string.Formatter().parse(value):
            if field_name is None:
                continue
            if field_name not in PLACEHOLDERS or conversion:
                raise TemplateError("template_invalid")
            if spec and (field_name not in _NUMERIC or not _SPEC.match(spec)):
                raise TemplateError("template_invalid")
            used.add(field_name)
    except ValueError as exc:  # unbalanced braces
        raise TemplateError("template_invalid") from exc
    if required not in used:
        raise TemplateError("template_missing")
    return value


def clean_part(text: str) -> str:
    """One file or folder name: no separators or characters a file system refuses."""
    text = unicodedata.normalize("NFC", text).replace("/", "-")
    text = _ILLEGAL.sub("_", text)
    text = " ".join(text.split()).strip(" .")
    while len(text.encode("utf-8")) > MAX_PART_BYTES:
        text = text[:-1].rstrip(" .")
    if not text:
        return "_"
    if text.split(".")[0].lower() in _RESERVED:
        text = f"_{text}"
    return text


@dataclass(frozen=True)
class NameValues:
    artist: str
    album: str
    year: int | None = None
    track: int | None = None
    disc: int | None = None
    title: str = ""


def _render(template: str, values: NameValues) -> str:
    data = {
        "artist": values.artist,
        "album": values.album,
        "year": values.year if values.year else "",
        "track": values.track or 0,
        "disc": values.disc or 1,
        "title": values.title,
    }

    class _Blank(dict):
        def __missing__(self, key):  # a numeric field that is empty keeps its width as text
            return ""

    # {year:02d} on an empty year would fail: format those as text
    if not data["year"]:
        template = re.sub(r"\{year(?::[^}]*)?\}", "{year}", template)
    return string.Formatter().vformat(template, (), _Blank(data))


def folder_parts(template: str, values: NameValues) -> list[str]:
    """The album folder as path parts (artist folder, album folder, ...). The template is split
    first: a "/" inside a value (AC/DC) never makes a folder of its own."""
    parts = []
    for part in template.split("/"):
        text = _EMPTY_BRACKETS.sub("", _render(part, values))
        if text.strip():
            parts.append(clean_part(text))
    return parts or ["_"]


def file_stem(template: str, values: NameValues, *, multi_disc: bool) -> str:
    """The file name without its extension. Albums on several discs get the disc number in
    front when the template does not have one, so that the files never collide."""
    if multi_disc and "{disc" not in template:
        template = f"{{disc}}-{template}"
    return clean_part(_EMPTY_BRACKETS.sub("", _render(template.replace("/", "-"), values)))
