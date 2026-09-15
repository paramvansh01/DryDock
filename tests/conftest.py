"""Load .env before any test module is imported.

drydock/config.py loads .env too, but only once something imports it. tests/test_invariants.py decides at import
time whether 18.11 and 18.16 can run (they need GEMINI_API_KEY), so without this they were skipped whenever the
key lived in .env rather than in the shell. Existing environment variables win, as in config.py.
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
