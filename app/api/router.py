from fastapi import APIRouter

from app.modules.admin.router import router as admin_router
from app.modules.auth.router import router as auth_router
from app.modules.projects.router import public_router
from app.modules.projects.router import router as projects_router
from app.modules.rbac.router import router as rbac_router
from app.modules.users.router import router as users_router

# Every HTTP route. The collaboration websocket is deliberately NOT here: this
# router is mounted with a request-scoped dependency that takes a `Request`,
# and FastAPI hands a websocket route a `WebSocket` instead — the dependency
# would never be filled and the socket would fail to open. It is mounted
# separately in `app.main`.
api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(rbac_router)
api_router.include_router(projects_router)
# Anonymous by design: the one route on it serves a project whose owner minted
# a public link. Mounted here rather than under /projects so the "everything in
# this prefix needs a signed-in caller" rule stays true of that prefix.
api_router.include_router(public_router)
api_router.include_router(admin_router)
