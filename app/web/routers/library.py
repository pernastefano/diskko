"""The library as users see it: artist grid/list with search, filters and infinite scroll."""

import math
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request

from app.auth.deps import DB, UserAuth
from app.services.library_views import (
    PAGE_SIZE,
    SORTS,
    available_letters,
    list_artists,
)
from app.services.musicbrainz.missing import get_missing_types
from app.web.templating import render

router = APIRouter()

VIEWS = ("grid", "list")


class LibraryParams:
    """Normalized filters of the library page; builds the query strings of its links."""

    def __init__(self, q: str, letter: str, sort: str, missing: str, view: str, page: int) -> None:
        self.q = q.strip()[:100]
        initial = letter.strip()[:1]
        self.letter = (
            initial.upper() if initial == "#" or (initial.isascii() and initial.isalpha()) else ""
        )
        self.sort = sort if sort in SORTS else "name"
        self.only_missing = missing == "1"
        self.view = view if view in VIEWS else "grid"
        self.page = max(page, 1)

    def query(self, **overrides: object) -> str:
        values = {
            "q": self.q,
            "letter": self.letter,
            "sort": self.sort if self.sort != "name" else "",
            "missing": "1" if self.only_missing else "",
            "view": self.view if self.view != "grid" else "",
            "page": self.page if self.page > 1 else "",
        }
        values.update(overrides)
        return urlencode({k: v for k, v in values.items() if v not in ("", None)})


def _params(
    q: str = "",
    letter: str = "",
    sort: str = "name",
    missing: str = "",
    view: str = "grid",
    page: int = 1,
) -> LibraryParams:
    return LibraryParams(q, letter, sort, missing, view, page)


def _context(db: DB, auth: UserAuth, params: LibraryParams) -> dict:
    rows, total = list_artists(
        db,
        types=get_missing_types(db, auth.user.id),
        q=params.q,
        letter=params.letter,
        sort=params.sort,
        only_missing=params.only_missing,
        page=params.page,
    )
    pages = max(math.ceil(total / PAGE_SIZE), 1)
    return {
        "rows": rows,
        "total": total,
        "page": params.page,
        "pages": pages,
        "p": params,
        "next_query": params.query(page=params.page + 1) if params.page < pages else None,
        "prev_query": params.query(page=params.page - 1 if params.page > 2 else "")
        if params.page > 1
        else None,
    }


@router.get("/library")
def library_page(
    request: Request, db: DB, auth: UserAuth, params: Annotated[LibraryParams, Depends(_params)]
):
    ctx = _context(db, auth, params)
    ctx["letters"] = available_letters(db)
    ctx["alphabet"] = [*"ABCDEFGHIJKLMNOPQRSTUVWXYZ", "#"]
    return render(request, "library.html", ctx)


@router.get("/library/items")
def library_items(
    request: Request, db: DB, auth: UserAuth, params: Annotated[LibraryParams, Depends(_params)]
):
    """Tiles of one page (HTMX): used by the search box and by the infinite scroll."""
    return render(request, "_library_items.html", _context(db, auth, params))
