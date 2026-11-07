"""The administrator's settings pages: library, integrations, jobs, albums and quality."""

import os
from itertools import islice
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth.deps import DB, AdminAuth, require_admin
from app.config import Settings
from app.quality import QUALITY_TIERS
from app.services.musicbrainz.categories import CATEGORIES
from app.services.musicbrainz.missing import get_missing_types, set_missing_types
from app.services.runtime_config import (
    FIELDS,
    QUEUES,
    Field,
    FieldError,
    apply_runtime_config,
    fields_of,
    overridden_keys,
    parse_value,
    save_values,
)
from app.web.routers.status import integration_card
from app.web.templating import render

router = APIRouter(prefix="/settings", dependencies=[Depends(require_admin)])

SECTIONS = ("library", "integrations", "jobs", "albums")
GROUPS = ("plex", "slskd", "musicbrainz", "coverart", "lastfm")
TESTABLE = ("plex", "slskd", "musicbrainz")  # integrations with a connection test
CHOICE_LABELS = {"library_source": "library.source.", "default_quality": "quality.full."}
FOLDER_LISTING_CAP = 100_000


def _display_default(base: Settings, field: Field) -> str:
    value = getattr(base, field.key)
    if field.kind == "secret":
        return ""
    if field.kind == "bool":
        return "yes" if value else "no"
    return str(value) if value not in ("", None) else ""


def field_view(
    request: Request,
    overridden: set[str],
    field: Field,
    submitted: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """What a template needs to draw one field. Secrets never leave this function."""
    settings: Settings = request.app.state.settings
    base: Settings = request.app.state.base_settings
    current = getattr(settings, field.key)
    view: dict[str, Any] = {
        "key": field.key,
        "kind": field.kind,
        "required": field.required,
        "overridden": field.key in overridden,
        "default": _display_default(base, field),
        "error": f"settings.error.{errors[field.key]}" if errors and field.key in errors else None,
        "choices": [(c, CHOICE_LABELS.get(field.key, "") + c) for c in field.choices],
        "secret_set": field.kind == "secret" and bool(current),
    }
    if field.kind == "secret":
        view["value"] = ""
    elif submitted is not None and field.key in submitted:
        view["value"] = submitted[field.key]
    elif field.kind == "bool":
        view["value"] = bool(current)
    else:
        view["value"] = str(current)
    if field.kind == "bool" and submitted is not None:
        view["value"] = field.key in submitted
    return view


def _fields(
    request: Request,
    db: DB,
    fields: list[Field],
    submitted: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    overridden = overridden_keys(db)
    return {f.key: field_view(request, overridden, f, submitted, errors) for f in fields}


def inspect_folder(path: str) -> tuple[str, int | None]:
    """('ok', top-level entries) or the reason the folder cannot be used. Never lists names."""
    folder = Path(path)
    if not folder.exists():
        return "not_found", None
    if not folder.is_dir():
        return "not_directory", None
    if not os.access(folder, os.R_OK | os.X_OK):
        return "not_readable", None
    try:
        return "ok", sum(1 for _ in islice(os.scandir(folder), FOLDER_LISTING_CAP))
    except OSError:
        return "not_readable", None


# ------------------------------------------------------------------------ pages


def _page(
    request: Request,
    db: DB,
    section: str,
    *,
    status_code: int = 200,
    submitted: dict[str, Any] | None = None,
    errors: dict[str, str] | None = None,
    error_group: str = "",
):
    settings: Settings = request.app.state.settings
    ctx: dict[str, Any] = {
        "section": section,
        "sections": SECTIONS,
        "errors": errors or {},
        "error_group": error_group,
    }
    if section == "library":
        ctx["fields"] = _fields(request, db, fields_of("library"), submitted, errors)
    elif section == "integrations":
        fields = _fields(request, db, fields_of("integrations"), submitted, errors)
        ctx["fields"] = fields
        ctx["groups"] = GROUPS
        ctx["group_fields"] = {g: [f.key for f in fields_of("integrations", g)] for g in GROUPS}
        ctx["cards"] = {n: integration_card(request, db, n) for n in TESTABLE}
    elif section == "jobs":
        concurrency = settings.queue_concurrency
        shown = {q: str(concurrency.get(q, 1)) for q in QUEUES}
        if submitted:
            shown.update({q: str(submitted.get(f"queue_{q}", shown[q])) for q in QUEUES})
        ctx["queues"] = [(q, shown[q], q == "musicbrainz") for q in QUEUES]
        ctx["fields"] = _fields(request, db, fields_of("jobs"), None, errors)
    elif section == "albums":
        ctx["fields"] = _fields(request, db, fields_of("albums"), submitted, errors)
        ctx["categories"] = CATEGORIES
        ctx["selected_types"] = get_missing_types(db)
        ctx["tiers"] = QUALITY_TIERS
    else:  # pragma: no cover - guarded by the route
        raise HTTPException(status_code=404, detail="not_found")
    return render(request, f"settings/{section}.html", ctx, status_code=status_code)


@router.get("")
def settings_index():
    return RedirectResponse("/settings/library", status_code=303)


@router.get("/{section}")
def settings_page(request: Request, db: DB, section: str):
    if section not in SECTIONS:
        raise HTTPException(status_code=404, detail="not_found")
    return _page(request, db, section)


# ----------------------------------------------------------------------- saving


async def _save(
    request: Request,
    db: DB,
    section: str,
    fields: list[Field],
    *,
    error_group: str = "",
):
    form = await request.form()
    reset = set(form.getlist("reset")) & {f.key for f in fields}
    errors = save_values(db, fields, form, reset=reset)
    if errors:
        db.rollback()
        submitted = {k: v for k, v in form.items() if isinstance(v, str)}
        return _page(
            request,
            db,
            section,
            status_code=400,
            submitted=submitted,
            errors=errors,
            error_group=error_group,
        )
    db.commit()
    apply_runtime_config(request.app)
    return RedirectResponse(f"/settings/{section}?flash=settings_saved", status_code=303)


@router.post("/library/check")
async def check_library_folder(request: Request):
    """Does the folder exist and can the app read it? (a quick look, never a scan)"""
    form = await request.form()
    raw = str(form.get("music_dir", ""))
    try:
        path = parse_value(FIELDS["music_dir"], raw)
    except FieldError as exc:
        return render(request, "settings/_path_check.html", {"state": "invalid", "code": exc.code})
    state, entries = inspect_folder(path)
    return render(request, "settings/_path_check.html", {"state": state, "entries": entries})


@router.post("/library")
async def save_library(request: Request, db: DB, auth: AdminAuth):
    return await _save(request, db, "library", fields_of("library"))


@router.post("/integrations/{group}")
async def save_integration(request: Request, db: DB, auth: AdminAuth, group: str):
    if group not in GROUPS:
        raise HTTPException(status_code=404, detail="not_found")
    return await _save(
        request, db, "integrations", fields_of("integrations", group), error_group=group
    )


@router.post("/jobs")
async def save_jobs(request: Request, db: DB, auth: AdminAuth):
    return await _save(request, db, "jobs", fields_of("jobs"))


@router.post("/albums/quality")
async def save_quality(request: Request, db: DB, auth: AdminAuth):
    return await _save(request, db, "albums", fields_of("albums"))


@router.post("/albums/types")
async def save_missing_types(request: Request, db: DB, auth: AdminAuth):
    form = await request.form()
    set_missing_types(db, form.getlist("types"))
    db.commit()
    return RedirectResponse("/settings/albums?flash=settings_saved", status_code=303)
