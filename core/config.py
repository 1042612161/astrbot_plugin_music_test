# typed_config.py
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_path,
    get_astrbot_temp_path,
)


class ConfigNode:
    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] miss key: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"key {key} need dict but {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        return MappingProxyType(self._data)

    def save_config(self) -> None:

        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() only support AstrBotConfig"
            )
        self._data.save_config()


class PluginConfig(ConfigNode):
    default_player_name: str
    nodejs_base_url: str
    song_limit: int
    select_modes: list[str]
    cards_per_row: int
    send_modes: str
    cz_ckey: str
    record_unsupported: list[str]
    file_unsupported: list[str]
    enable_comments: bool
    proxy: str
    timeout: int
    recall_select: bool
    render_font: str
    render_emoji_font: str
    render_other_font: str
    enc_sec_key: str
    enc_params: str

    _plugin_name: str = "astrbot_plugin_music_test"

    def __init__(self, config: AstrBotConfig, context: Context):
        super().__init__(config)
        self.context = context
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        fonts_dir = self.plugin_dir / "fonts"
        configured_fonts = (
            ("primary", self.render_font),
            ("emoji", self.render_emoji_font),
            ("other", self.render_other_font),
        )
        self.font_role_paths: dict[str, Path] = {}
        for role, configured in configured_fonts:
            path = self._font_path(fonts_dir, configured, role)
            if path is not None:
                self.font_role_paths[role] = path
        self.font_path = self.font_role_paths.get("primary")
        if self.font_path is None:
            raise FileNotFoundError("主渲染字体未配置或文件不存在")
        self.temp_dir = Path(get_astrbot_temp_path()) / self._plugin_name
        self.songs_dir = self.temp_dir / "songs"
        self.songs_dir.mkdir(parents=True, exist_ok=True)

        self._select_modes = [m.split("(", 1)[0].strip() for m in self.select_modes]

        # 发送方式改为单选。兼容旧版曾保存的列表配置，优先取其中第一个
        # 仍支持的模式，且只允许两种不会触发 AstrBot WAV/Base64 转换链路的方式。
        raw_send_mode: Any = self.send_modes
        legacy_modes = raw_send_mode if isinstance(raw_send_mode, list) else [raw_send_mode]
        send_mode = ""
        for candidate in legacy_modes:
            parsed = str(candidate or "").split("(", 1)[0].strip()
            if parsed in {"record_link", "file_link"}:
                send_mode = parsed
                break
        if not send_mode and legacy_modes:
            send_mode = str(legacy_modes[0] or "").split("(", 1)[0].strip()
        if send_mode not in {"record_link", "file_link"}:
            if send_mode:
                logger.warning(
                    f"发送模式 {send_mode} 不受支持，已改用默认语音链接模式"
                )
            send_mode = "record_link"
        self._send_mode = send_mode

    @staticmethod
    def _font_path(
        fonts_dir: Path,
        configured: str | None,
        role: str,
    ) -> Path | None:
        value = str(configured or "").strip()
        if not value:
            logger.warning(f"{role} 字体未配置，已跳过")
            return None
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            logger.warning(f"{role} 字体必须是 fonts 目录下的相对路径，已跳过")
            return None
        path = fonts_dir / relative
        if not path.is_file():
            logger.warning(f"{role} 字体文件不存在，已跳过: {relative}")
            return None
        return path

    @property
    def real_select_modes(self) -> list[str]:
        return self._select_modes

    @property
    def http_proxy(self) -> str | None:
        return self.proxy or None

    @property
    def real_send_modes(self) -> list[str]:
        # 保留列表形式的兼容属性，发送器只会消费其中唯一的一个模式。
        return [self._send_mode]

    @property
    def real_send_mode(self) -> str:
        return self._send_mode

    @property
    def real_song_limit(self) -> int:
        return (
            1
            if self._select_modes and self._select_modes[0] == "single"
            else self.song_limit
        )
