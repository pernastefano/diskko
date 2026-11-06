from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth.sessions import load_session
from app.config import Settings
from app.db import get_db
from app.models import User, UserSession

DB = Annotated[Session, Depends(get_db)]


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


class LoginRequired(Exception):
    """Raised when an authenticated user is needed; handled in app/web/errors.py."""


@dataclass
class AuthContext:
    user: User
    session: UserSession


def resolve_auth(request: Request, db: Session) -> AuthContext | None:
    """Resolve (once per request) the current session/user from the cookie."""
    if hasattr(request.state, "auth"):
        return request.state.auth
    settings: Settings = request.app.state.settings
    auth = None
    cookie = request.cookies.get(settings.session_cookie_name)
    if cookie:
        row, refreshed = load_session(db, cookie, settings)
        if row is not None:
            auth = AuthContext(user=row.user, session=row)
            if refreshed:
                request.state.refresh_session_cookie = cookie
    request.state.auth = auth
    return auth


def optional_auth(request: Request, db: DB) -> AuthContext | None:
    return resolve_auth(request, db)


def require_user(auth: Annotated[AuthContext | None, Depends(optional_auth)]) -> AuthContext:
    if auth is None:
        raise LoginRequired
    return auth


def require_admin(auth: Annotated[AuthContext, Depends(require_user)]) -> AuthContext:
    if not auth.user.is_admin:
        raise HTTPException(status_code=403, detail="forbidden")
    return auth


OptionalAuth = Annotated[AuthContext | None, Depends(optional_auth)]
UserAuth = Annotated[AuthContext, Depends(require_user)]
AdminAuth = Annotated[AuthContext, Depends(require_admin)]
