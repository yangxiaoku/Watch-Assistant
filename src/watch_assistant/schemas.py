"""Shared resource and task value types."""

from enum import StrEnum


class ResourceKind(StrEnum):
    MAGNET = "magnet"
    SHARE = "share"


class TaskAction(StrEnum):
    OFFLINE_DOWNLOAD = "offline_download"
    SAVE_SHARE = "save_share"
