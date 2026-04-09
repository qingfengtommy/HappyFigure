"""HappyFigure terminal UI package.

Re-exports all public names from ui.app (the main UI module) so that
``import ui; ui.banner(...)`` continues to work unchanged.

Stream parsers are available via ``from ui.stream_parsers import ...``.
"""
# Re-export everything from the main UI module (including private helpers
# that tests access) so ``import ui; ui.banner(...)`` keeps working.
import ui.app as _app

def __getattr__(name):
    return getattr(_app, name)

# Make `from ui import *` work — pull __all__ or public names from app
from ui.app import *  # noqa: F401,F403
