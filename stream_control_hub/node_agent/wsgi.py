"""Gunicorn entrypoint for the stream node agent."""

from .app import APP, start_background_services

start_background_services()
