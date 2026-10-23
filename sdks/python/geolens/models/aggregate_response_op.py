from typing import Literal, cast

AggregateResponseOp = Literal["count", "group_by", "histogram", "stats", "value_counts"]

AGGREGATE_RESPONSE_OP_VALUES: set[AggregateResponseOp] = {
    "count",
    "group_by",
    "histogram",
    "stats",
    "value_counts",
}


def check_aggregate_response_op(value: str) -> AggregateResponseOp:
    if value in AGGREGATE_RESPONSE_OP_VALUES:
        return cast(AggregateResponseOp, value)
    raise TypeError(
        f"Unexpected value {value!r}. Expected one of {AGGREGATE_RESPONSE_OP_VALUES!r}"
    )
