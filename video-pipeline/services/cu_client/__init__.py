"""Child-process client for the pinned metacua-go Computer Use agent.

§1.1 discipline: this package is justified by suda's direct instruction
plus measured necessity (CU-only capabilities exist — capability index
S05/S06/S07/S09 etc.); it is NOT a precedent for other abstraction layers,
and carries no app restrictions or step limits by suda's explicit
instruction. See ``client`` for the timeout and trace-impermanence design
notes, ``errors`` for the recorded CuTimeoutError deviation, and ``ime``
for the paired IME switch/restore design notes (readback verification,
kill-path honesty, restore-on-failed-switch policy).
"""

from __future__ import annotations

from services.cu_client.ascii_entry import type_ascii
from services.cu_client.client import (
    CU_WINDOW_RESOURCE,
    DEFAULT_CU_PIN_PATH,
    CuClient,
    CuVerifier,
    cu_window,
)
from services.cu_client.errors import (
    CuClientError,
    CuLaunchError,
    CuLeaseError,
    CuTraceCollectionError,
    GuiOccupiedError,
    ImeError,
    ImeReadError,
    ImeRestoreError,
    ImeSelectError,
    PreflightError,
    ReadbackUndefinedError,
)
from services.cu_client.gui_marker import DEFAULT_MARKER_PATH, GuiMarker
from services.cu_client.ime import (
    ENGLISH_SOURCE_ID,
    current_input_source,
    english_typing,
)
from services.cu_client.live_coords import resolve_center
from services.cu_client.models import CuPin, CuResult
from services.cu_client.preflight import (
    PreflightActions,
    PreflightReport,
    PreflightSpec,
    SelectionSpec,
    run_preflight,
)
from services.cu_client.readbacks import READBACKS, ReadbackSpec, readback_for

__all__ = [
    "CU_WINDOW_RESOURCE",
    "DEFAULT_CU_PIN_PATH",
    "DEFAULT_MARKER_PATH",
    "ENGLISH_SOURCE_ID",
    "READBACKS",
    "CuClient",
    "CuClientError",
    "CuLaunchError",
    "CuLeaseError",
    "CuPin",
    "CuResult",
    "CuTraceCollectionError",
    "CuVerifier",
    "GuiMarker",
    "GuiOccupiedError",
    "ImeError",
    "ImeReadError",
    "ImeRestoreError",
    "ImeSelectError",
    "PreflightActions",
    "PreflightError",
    "PreflightReport",
    "PreflightSpec",
    "ReadbackSpec",
    "ReadbackUndefinedError",
    "SelectionSpec",
    "cu_window",
    "current_input_source",
    "english_typing",
    "readback_for",
    "resolve_center",
    "run_preflight",
    "type_ascii",
]
