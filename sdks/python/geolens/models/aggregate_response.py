from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, TYPE_CHECKING

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.aggregate_response_op import AggregateResponseOp
from ..models.aggregate_response_op import check_aggregate_response_op

if TYPE_CHECKING:
    from ..models.count_result import CountResult
    from ..models.group_by_result import GroupByResult
    from ..models.histogram_result import HistogramResult
    from ..models.stats_result import StatsResult
    from ..models.value_counts_result import ValueCountsResult


T = TypeVar("T", bound="AggregateResponse")


@_attrs_define
class AggregateResponse:
    """
    Attributes:
        op (AggregateResponseOp):
        result (CountResult | GroupByResult | HistogramResult | StatsResult | ValueCountsResult):
        cached (bool):
        approximate (bool | Unset): Always false: a view too wide to count exactly is refused. Default: False.
    """

    op: AggregateResponseOp
    result: (
        CountResult | GroupByResult | HistogramResult | StatsResult | ValueCountsResult
    )
    cached: bool
    approximate: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.count_result import CountResult
        from ..models.histogram_result import HistogramResult
        from ..models.stats_result import StatsResult
        from ..models.value_counts_result import ValueCountsResult

        op: str = self.op

        result: dict[str, Any]
        if isinstance(self.result, CountResult):
            result = self.result.to_dict()
        elif isinstance(self.result, StatsResult):
            result = self.result.to_dict()
        elif isinstance(self.result, ValueCountsResult):
            result = self.result.to_dict()
        elif isinstance(self.result, HistogramResult):
            result = self.result.to_dict()
        else:
            result = self.result.to_dict()

        cached = self.cached

        approximate = self.approximate

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "op": op,
                "result": result,
                "cached": cached,
            }
        )
        if approximate is not UNSET:
            field_dict["approximate"] = approximate

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.count_result import CountResult
        from ..models.group_by_result import GroupByResult
        from ..models.histogram_result import HistogramResult
        from ..models.stats_result import StatsResult
        from ..models.value_counts_result import ValueCountsResult

        d = dict(src_dict)
        op = check_aggregate_response_op(d.pop("op"))

        def _parse_result(
            data: object,
        ) -> (
            CountResult
            | GroupByResult
            | HistogramResult
            | StatsResult
            | ValueCountsResult
        ):
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = CountResult.from_dict(data)

                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_1 = StatsResult.from_dict(data)

                return result_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_2 = ValueCountsResult.from_dict(data)

                return result_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_3 = HistogramResult.from_dict(data)

                return result_type_3
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            result_type_4 = GroupByResult.from_dict(data)

            return result_type_4

        result = _parse_result(d.pop("result"))

        cached = d.pop("cached")

        approximate = d.pop("approximate", UNSET)

        aggregate_response = cls(
            op=op,
            result=result,
            cached=cached,
            approximate=approximate,
        )

        aggregate_response.additional_properties = d
        return aggregate_response

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
