"""Application assembly: logging, the FastAPI app instance, static file mount, and router
inclusion. Mirrors foil-serve's main.py — no routes and no response shaping live here; the HTTP
surface lives in api.py (static routes) and alias_routes.py (routes generated from
backend_config.toml aliases), and the shared run()/response-shaping pipeline they both use lives
in responders.py.
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi_offline import FastAPIOffline

import alias_routes
import api
import auto_routes
from config.settings import setup_logging
from storage import lifespan

setup_logging()

app: FastAPI = FastAPIOffline(
    title="MarkGate",
    description="""
<div align="center">
    <img src="/statics/markgate_banner.jpg" alt="MarkGate Banner" width="500" />
    </br>
    <b>MarkGate</b>, a proxy for Markdown converter backends with persistent and versioned cache.
</div>
    """,
    favicon_url="/favicon.ico",
    lifespan=lifespan,
)

app.mount("/statics", StaticFiles(directory=api.STATICS_DIR), name="statics")

# auto_routes must be included before api.router: /md/auto/process etc. are literal paths that
# would otherwise be shadowed by api.router's /md/{version}/process — Starlette matches routes by
# path shape in registration order and doesn't fall through to a later route once one has matched,
# so "auto" would be rejected as an invalid Version instead of ever reaching auto_routes' route.
# See auto_routes.py's module docstring. build_router() returns None when AUTO_ROUTE_ENABLED is
# false, in which case the routes simply don't exist.
if auto_router := auto_routes.build_router():
    app.include_router(auto_router)
app.include_router(api.router)
app.include_router(alias_routes.build_router())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
