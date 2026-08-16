from collections.abc import Callable

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type PolicyPayload = dict[str, JsonValue]
type PolicyPayloadFactory = Callable[[], PolicyPayload]
