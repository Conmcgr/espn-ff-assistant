"""Small private dashboard, served behind Devin's sign-in proxy (not public hosting)."""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock
from typing import Any

from flask import Flask, jsonify

from .data import load_section

log = logging.getLogger(__name__)
SECTIONS = frozenset({"lineup", "waivers", "trades"})


@dataclass
class Entry:
    future: Future | None = None
    value: dict[str, Any] | None = None
    expires: float = 0
    failed: bool = False


def create_app(loader=load_section, *, ttl: float = 300) -> Flask:
    app = Flask(__name__, static_url_path="/assets")
    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="dashboard-read")
    cache: dict[str, Entry] = {}
    lock = Lock()

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; frame-ancestors 'self' https://*.devin.ai; "
            "base-uri 'none'; form-action 'none'; object-src 'none'"
        )
        return response

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/<section>")
    def section_data(section):
        if section not in SECTIONS:
            return jsonify(error="Not found"), 404
        with lock:
            entry = cache.setdefault(section, Entry())
            now = time.monotonic()
            if entry.future is not None and entry.future.done():
                try:
                    entry.value = entry.future.result()
                    entry.failed = False
                    entry.expires = now + ttl
                except Exception as error:
                    # Never log exception text: connection errors can contain credentials.
                    log.warning("Dashboard %s unavailable (%s)", section, type(error).__name__)
                    entry.failed = True
                    entry.expires = now + 60
                entry.future = None
            if entry.future is None and now >= entry.expires:
                entry.future = pool.submit(loader, section)
            if entry.value is not None:
                return jsonify(
                    **entry.value, refreshing=entry.future is not None,
                    refresh_failed=entry.failed,
                )
            if entry.failed:
                return jsonify(error="League data is temporarily unavailable. Try again in a minute."), 503
            return jsonify(status="loading"), 202

    return app
