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
    ImeError,
    ImeReadError,
    ImeRestoreError,
    ImeSelectError,
)
from services.cu_client.ime import (
    ENGLISH_SOURCE_ID,
    current_input_source,
    english_typing,
)
from services.cu_client.models import CuPin, CuResult

__all__ = [
    "CU_WINDOW_RESOURCE",
    "DEFAULT_CU_PIN_PATH",
    "ENGLISH_SOURCE_ID",
    "CuClient",
    "CuClientError",
    "CuLaunchError",
    "CuLeaseError",
    "CuPin",
    "CuResult",
    "CuTraceCollectionError",
    "CuVerifier",
    "ImeError",
    "ImeReadError",
    "ImeRestoreError",
    "ImeSelectError",
    "cu_window",
    "current_input_source",
    "english_typing",
]
