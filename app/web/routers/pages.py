from fastapi import APIRouter, Request

from app.auth.deps import UserAuth
from app.web.templating import render

router = APIRouter()


@router.get("/")
def home(request: Request, auth: UserAuth):
    return render(request, "home.html")
