from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.auth.deps import UserAuth

router = APIRouter()


@router.get("/")
def home(auth: UserAuth):
    """The library is the home page."""
    return RedirectResponse("/library", status_code=303)
