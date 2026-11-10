#!/usr/bin/env python3
"""Media preservation budgets: media.yaml, CLI overrides and strict validation.

Resolution order is explicit CLI override > ``media.yaml`` > defaults.  Values
are validated here, once, so every caller gets the same bounded budget and a
bad configuration fails before any network request.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

MIB = 1024 * 1024

DEFAULT_MAX_IMAGES = 100
DEFAULT_MAX_SINGLE_IMAGE_MIB = 10
DEFAULT_MAX_TOTAL_IMAGE_MIB = 50
DEFAULT_MAX_DOWNLOAD_SECONDS = 180.0

# Upper bounds keep a configuration mistake from defeating the streaming /
# reader object limits.  16 MiB per image stays below the 20 MiB published
# object limit used by the encrypted reader.
MAX_IMAGES_LIMIT = 1000
MAX_SINGLE_IMAGE_MIB_LIMIT = 16
MAX_TOTAL_IMAGE_MIB_LIMIT = 500
MAX_DOWNLOAD_SECONDS_LIMIT = 3600

CONFIG_FILENAME = "media.yaml"
KNOWN_KEYS = (
    "max_images",
    "max_single_image_mib",
    "max_total_image_mib",
    "max_download_seconds",
)


class MediaConfigError(ValueError):
    """Invalid media budget configuration."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of last-wins."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node, deep: bool = False):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise MediaConfigError(f"media.yaml 含重复配置项: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class MediaBudget:
    max_images: int
    max_single_image_bytes: int
    max_total_image_bytes: int
    max_download_seconds: float

    @property
    def max_single_image_mib(self) -> float:
        return self.max_single_image_bytes / MIB

    @property
    def max_total_image_mib(self) -> float:
        return self.max_total_image_bytes / MIB

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_images": self.max_images,
            "max_single_image_mib": _trim_number(self.max_single_image_mib),
            "max_total_image_mib": _trim_number(self.max_total_image_mib),
            "max_download_seconds": _trim_number(self.max_download_seconds),
        }


def _trim_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(float(value), 3)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive(value: Any, name: str, *, maximum: float, integer: bool = False) -> float:
    if not _is_number(value):
        raise MediaConfigError(f"{name} 必须是数字，当前为 {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise MediaConfigError(f"{name} 必须是有限数字，当前为 {value!r}")
    if integer and not number.is_integer():
        raise MediaConfigError(f"{name} 必须是整数，当前为 {value!r}")
    if number <= 0:
        raise MediaConfigError(f"{name} 必须大于 0，当前为 {value!r}")
    if number > maximum:
        raise MediaConfigError(f"{name} 不能超过 {_trim_number(maximum)}，当前为 {value!r}")
    return number


def _budget_from_values(values: Mapping[str, Any]) -> MediaBudget:
    unknown = sorted(set(values) - set(KNOWN_KEYS))
    if unknown:
        raise MediaConfigError(f"media.yaml 含未知配置项: {', '.join(unknown)}")

    max_images = int(_positive(
        values.get("max_images", DEFAULT_MAX_IMAGES),
        "max_images",
        maximum=MAX_IMAGES_LIMIT,
        integer=True,
    ))
    single_mib = _positive(
        values.get("max_single_image_mib", DEFAULT_MAX_SINGLE_IMAGE_MIB),
        "max_single_image_mib",
        maximum=MAX_SINGLE_IMAGE_MIB_LIMIT,
    )
    total_mib = _positive(
        values.get("max_total_image_mib", DEFAULT_MAX_TOTAL_IMAGE_MIB),
        "max_total_image_mib",
        maximum=MAX_TOTAL_IMAGE_MIB_LIMIT,
    )
    seconds = _positive(
        values.get("max_download_seconds", DEFAULT_MAX_DOWNLOAD_SECONDS),
        "max_download_seconds",
        maximum=MAX_DOWNLOAD_SECONDS_LIMIT,
    )
    return MediaBudget(
        max_images=max_images,
        max_single_image_bytes=int(round(single_mib * MIB)),
        max_total_image_bytes=int(round(total_mib * MIB)),
        max_download_seconds=float(seconds),
    )


def project_root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root)
    return Path(__file__).resolve().parent.parent


def load_media_config(root: Path | str | None = None) -> MediaBudget:
    """Load ``media.yaml`` from ``root``; a missing file means defaults."""
    base = project_root(root)
    path = base / CONFIG_FILENAME
    if not path.exists():
        return _budget_from_values({})
    if not path.is_file():
        raise MediaConfigError(f"{path} 不是普通文件")
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except MediaConfigError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise MediaConfigError(f"无法读取 {path}: {exc}") from exc
    if raw is None:
        return _budget_from_values({})
    if not isinstance(raw, dict):
        raise MediaConfigError(f"{path} 顶层必须是映射（key: value）")
    return _budget_from_values(raw)


def resolve_budget(
    overrides: Mapping[str, Any] | None = None,
    root: Path | str | None = None,
) -> MediaBudget:
    """Merge CLI overrides over ``media.yaml`` over defaults.

    ``None`` values in ``overrides`` mean "not supplied"; MiB inputs are
    converted to bytes only after validation.
    """
    base = load_media_config(root)
    supplied = {k: v for k, v in (overrides or {}).items() if v is not None}
    if not supplied:
        return base

    unknown = sorted(set(supplied) - set(KNOWN_KEYS))
    if unknown:
        raise MediaConfigError(f"未知的预算参数: {', '.join(unknown)}")

    values: dict[str, Any] = {
        "max_images": base.max_images,
        "max_single_image_mib": base.max_single_image_mib,
        "max_total_image_mib": base.max_total_image_mib,
        "max_download_seconds": base.max_download_seconds,
    }
    values.update(supplied)
    return _budget_from_values(values)
