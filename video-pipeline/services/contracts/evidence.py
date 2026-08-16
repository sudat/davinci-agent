from __future__ import annotations

from typing import Annotated

from pydantic import Field, TypeAdapter

from services.contracts.build_report import BuildReport0A
from services.contracts.timeline_ir import TimelineIr0A

Evidence0A = Annotated[TimelineIr0A | BuildReport0A, Field(discriminator="artifact_type")]
EVIDENCE_0A_ADAPTER: TypeAdapter[Evidence0A] = TypeAdapter(Evidence0A)
