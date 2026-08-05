"""kahin.tools — engine-separated category tool modules.

Importing this package registers every tool on the shared ``mcp`` instance
(defined in :mod:`kahin.oracle`) via ``@mcp.tool`` side effects.
``kahin/oracle.py`` imports this package at the end of its bootstrap; the
tool modules import ``mcp`` back from ``kahin.oracle``.

Engine-agnostic categories are shared files; Obscura/Camoufox-specific
categories live in the ``*_obscura.py`` / ``*_mirage.py`` stubs (filled in
by later phases).
"""

from kahin.tools import accessibility_mirage  # noqa: F401
from kahin.tools import dejavu  # noqa: F401
from kahin.tools import dejavu_mirage  # noqa: F401
from kahin.tools import dejavu_obscura  # noqa: F401
from kahin.tools import dialog_mirage  # noqa: F401
from kahin.tools import dom_stream_mirage  # noqa: F401
from kahin.tools import emulation_mirage  # noqa: F401
from kahin.tools import engine  # noqa: F401
from kahin.tools import grimoire  # noqa: F401
from kahin.tools import healer  # noqa: F401
from kahin.tools import pilot  # noqa: F401
from kahin.tools import pilot_mirage  # noqa: F401
from kahin.tools import pilot_obscura  # noqa: F401
from kahin.tools import prophecy  # noqa: F401
from kahin.tools import screencast_mirage  # noqa: F401
from kahin.tools import seraph  # noqa: F401
from kahin.tools import storage_mirage  # noqa: F401
from kahin.tools import trainman  # noqa: F401
from kahin.tools import trainman_mirage  # noqa: F401
from kahin.tools import trainman_obscura  # noqa: F401
from kahin.tools import upload_mirage  # noqa: F401

__all__ = [
    "accessibility_mirage",
    "dejavu",
    "dejavu_mirage",
    "dejavu_obscura",
    "dialog_mirage",
    "dom_stream_mirage",
    "emulation_mirage",
    "engine",
    "grimoire",
    "healer",
    "pilot",
    "pilot_mirage",
    "pilot_obscura",
    "prophecy",
    "screencast_mirage",
    "seraph",
    "storage_mirage",
    "trainman",
    "trainman_mirage",
    "trainman_obscura",
    "upload_mirage",
]
