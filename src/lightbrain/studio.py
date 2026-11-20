"""Serve the same graph client locally and in the incubator."""
from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


WEB = Path(__file__).resolve().parent / "web"
POLICY = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


def mount_studio(app):
    app.mount("/studio-assets", StaticFiles(directory=WEB), name="studio-assets")

    @app.get("/studio", include_in_schema=False)
    def studio():
        return FileResponse(WEB / "index.html", headers={
            "Content-Security-Policy": POLICY, "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
        })
