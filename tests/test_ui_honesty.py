"""The UI is not a canned demo: it defaults to the live event stream, the illustrative
fixture is dev-only and absent from production builds, and any replay is labelled.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "ui"


def test_default_source_is_live():
    app = (UI / "src" / "App.tsx").read_text()
    body = app[app.index("function initialSource"):app.index("export default function App")]
    assert body.strip().endswith('return { kind: "live" };\n}') or 'return { kind: "live" };' in body.splitlines()[-2]
    assert 'r === "fixture" && DEV' in body            # fixture only in the dev server


def test_fixture_is_excluded_from_production_builds():
    cfg = (UI / "vite.config.ts").read_text()
    assert re.search(r'publicDir:\s*command === "build"\s*\?\s*false', cfg)


def test_replays_are_watermarked_and_read_only():
    app = (UI / "src" / "App.tsx").read_text()
    assert "not a real run" in app and "ILLUSTRATIVE FIXTURE" in app
    assert "const canAct = !replay" in app


def test_no_component_fetches_data_directly():
    """The reducer is a pure function of the event stream: components never fetch state,
    they only call the reviewer API (whose effects come back as events)."""
    for p in (UI / "src").rglob("*.tsx"):
        src = p.read_text()
        assert "fetch(" not in src, p.name
