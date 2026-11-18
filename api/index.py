"""Vercel serverless entrypoint. Vercel's Python runtime serves the ASGI `app` it finds here."""

from app.main import app  # noqa: F401
