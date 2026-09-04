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

app.include_router(api.router)
app.include_router(alias_routes.build_router())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
