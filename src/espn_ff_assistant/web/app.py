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


def create_app(loader=load_section, *, ttl: float = 300, prewarm: bool = False) -> Flask:
    app = Flask(__name__, static_url_path="/assets")
    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="dashboard-read")
    cache: dict[str, Entry] = {}
    lock = Lock()
    if prewarm:
        cache = {section: Entry(future=pool.submit(loader, section)) for section in SECTIONS}

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
                value = entry.value
                # A cached result (including stale-while-refreshing) cannot extend
                # ESPN's exact trade window. Never return expired offers.
                if section == "trades":
                    data = value["data"]
                    deadline = data.get("deadline")
                    if deadline is not None and time.time() >= deadline.timestamp():
                        value = {**value, "data": {**data, "ideas": [], "notice":
                            "Your league's trade deadline has passed. No actionable trades are shown."}}
                return jsonify(
                    **value, refreshing=entry.future is not None,
                    refresh_failed=entry.failed,
                )
            if entry.failed:
                return jsonify(error="League data is temporarily unavailable. Try again in a minute."), 503
            return jsonify(status="loading"), 202

    @app.get("/ready")
    def ready():
        # Deploy only after all tabs have a result. This shares the API's cache,
        # refresh handling and deadline checks; it returns no private league data.
        statuses = [app.make_response(section_data(section)).status_code for section in SECTIONS]
        complete = all(status == 200 for status in statuses)
        return jsonify(ready=complete), 200 if complete else 503

    return app
