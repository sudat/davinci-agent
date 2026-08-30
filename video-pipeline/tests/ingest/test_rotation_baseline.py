"""T9 baseline characterization: intended rotation-metadata interpretation.

Freeze the CURRENT (pre-T9-fix) behavior of the display-matrix seam the
orientation QC builds on: absent side data means upright (None), only the
four exact canonical tkhd matrices map to degrees, and everything else is
refused as None (never guessed). This must stay green after the T9 fix —
the QC orientation check reuses this exact interpretation.
"""

from __future__ import annotations

from typing import Any

from services.ingest.records import ROTATION_MATRICES, rotation_degrees


def _stream_with_matrix(matrix: tuple[int, ...]) -> dict[str, Any]:
    return {
        "side_data_list": [
            {
                "side_data_type": "Display Matrix",
                "displaymatrix": "\n".join(
                    f"{row}: {a:8d} {b:8d} {c:8d}"
                    for row, (a, b, c) in enumerate(
                        zip(matrix[0::3], matrix[1::3], matrix[2::3], strict=True)
                    )
                ),
            }
        ]
    }


def test_no_side_data_means_upright_none() -> None:
    assert rotation_degrees({"width": 3840}) is None


def test_each_canonical_matrix_maps_to_its_degrees() -> None:
    for degrees, matrix in ROTATION_MATRICES.items():
        assert rotation_degrees(_stream_with_matrix(matrix)) == degrees


def test_noncanonical_matrix_is_refused_as_none() -> None:
    skewed = (32768, 0, 0, 0, 65536, 0, 0, 0, 65536)
    assert rotation_degrees(_stream_with_matrix(skewed)) is None


def test_unrelated_side_data_does_not_pretend_to_rotate() -> None:
    stream: dict[str, Any] = {
        "side_data_list": [{"side_data_type": "HDR Mastering Display Metadata"}]
    }
    assert rotation_degrees(stream) is None

