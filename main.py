import json
import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.wecom_ai_bot.WXBizJsonMsgCrypt import throw_exception
from astrbot.core.utils.session_waiter import (
    SessionController,
    session_waiter,
)

from .core.config import PluginConfig
from .core.downloader import Downloader
from .core.model import Song
from .core.platform import BaseMusicPlayer
from .core.sender import MusicSender
from .core.song_renderer import CardRenderer
from .core.utils import parse_user_input


_LLM_TERMINAL_ERROR = "啧，终端刚刚抽风了，关键时候掉链子。"


class MusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)
        self.song_renderer = CardRenderer(self.cfg)
        self.downloader = Downloader(self.cfg)
        self.sender = MusicSender(
            self.cfg,
            self.context,
            self.downloader,
            self.song_renderer,
        )
        self.players: list[BaseMusicPlayer] = []
        self.keywords: list[str] = []

    async def initialize(self):
        self._register_player()

    async def terminate(self):
        await self.sender.close()
        await self.downloader.close()
        for parser in self.players:
            await parser.close()

    def get_player(
        self, name: str | None = None, word: str | None = None, default: bool = False
    ) -> BaseMusicPlayer | None:
        if default:
            word = self.cfg.default_player_name
        for player in self.players:
            if name:
                name_ = name.strip().lower()
                p = player.platform
                if (
                    p.display_name.lower() == name_
                    or p.name.lower() == name_
                    or any(keyword.lower() == name_ for keyword in p.keywords)
                ):
                    return player
            elif word:
                word_ = word.strip().lower()
                for keyword in player.platform.keywords:
                    if keyword.lower() in word_:
                        return player

    def _register_player(self):
        """注册音乐播放器"""
        all_subclass = BaseMusicPlayer.get_all_subclass()
        for _cls in all_subclass:
            player = _cls(self.cfg)
            self.players.append(player)
            self.keywords.extend(player.platform.keywords)
        logger.debug(f"已注册触发词：{self.keywords}")

    @filter.command(
        "点歌",
        alias={
            "网易点歌",
            "网易nj",
            "QQ点歌",
            "酷狗点歌",
            "酷我点歌",
            "百度点歌",
            "一听点歌",
            "咪咕点歌",
            "荔枝点歌",
            "蜻蜓点歌",
            "喜马拉雅",
            "5sing原创",
            "5sing翻唱",
            "全民K歌",
        },
    )
    async def search_song(self, event: AstrMessageEvent):
        """点歌、网易点歌、网易nj、QQ点歌、酷狗点歌、酷我点歌、百度点歌、一听点歌、咪咕点歌、荔枝点歌、蜻蜓点歌、喜马拉雅、5sing原创、5sing翻唱、全民K歌 <搜索词>"""
        # 此函数仅用于注册显示命令
        pass

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_search_song(self, event: AstrMessageEvent):
        """监听点歌命令： 点歌、网易点歌、网易nj、QQ点歌、酷狗点歌、酷我点歌、百度点歌、一听点歌、咪咕点歌、荔枝点歌、蜻蜓点歌、喜马拉雅、5sing原创、5sing翻唱、全民K歌"""
        # 解析参数
        if not event.is_at_or_wake_command:
            return
        cmd, _, arg = event.message_str.partition(" ")
        if not arg:
            return
        player = self.get_player(word=cmd)
        if "点歌" == cmd:
            player = self.get_player(default=True)
        if not player:
            return
        args = arg.split()
        index: int = int(args[-1]) if args[-1].isdigit() else 0
        song_name = arg.removesuffix(str(index))
        if not song_name:
            yield event.plain_result("未指定歌名")
            return
        # 搜索歌曲
        logger.debug(f"正在通过{player.platform.display_name}搜索歌曲：{song_name}")
        songs = await player.fetch_songs(
            keyword=song_name, limit=self.cfg.real_song_limit, extra=cmd
        )
        if not songs:
            yield event.plain_result(f"搜索【{song_name}】无结果")
            return

        # 单曲模式
        if len(songs) == 1:
            index = 1

        # 输入了序号，直接发送歌曲
        if index and 0 <= index <= len(songs):
            selected_song = songs[int(index) - 1]
            await self.sender.send_song(event, player, selected_song)
            event.stop_event()
            return

        # 未提输入序号，等待用户选择歌曲
        selection_mode = await self.sender.send_song_selection(
            event=event, songs=songs, player=player
        )

        if selection_mode == "button":
            event.stop_event()
            return
        if selection_mode not in {"image", "text"}:
            self.sender.clear_selection_context(event)
            event.stop_event()
            return

        @session_waiter(timeout=self.cfg.timeout)
        async def empty_mention_waiter(
            controller: SessionController, event: AstrMessageEvent
        ):
            arg = event.message_str.strip()
            arg_lower = arg.lower()
            for kw in self.keywords:
                if kw in arg_lower:
                    controller.stop()
                    return
            # 解析输入格式
            index, modes, error = parse_user_input(arg)
            if error:
                await event.send(event.plain_result(error))
                return
            if index == 0:
                return
            if index < 1 or index > len(songs):
                controller.stop()
                return
            selected_song = songs[index - 1]
            controller.stop()
            await self.sender.send_song(event, player, selected_song, modes=modes)

        try:
            await empty_mention_waiter(event)
        except TimeoutError as _:
            self.sender.clear_selection_context(event)
            yield event.plain_result("点歌超时！")
        except Exception as e:
            logger.error(traceback.format_exc())
            logger.error("点歌发生错误" + str(e))

        event.stop_event()

    @staticmethod
    def _llm_tool_result(status: str, message: str, **data) -> str:
        return json.dumps(
            {"status": status, "message": message, **data},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def _send_song_for_llm(
        self,
        event: AstrMessageEvent,
        player: BaseMusicPlayer,
        song: Song,
    ) -> str:
        try:
            result = await self.sender.send_song(event, player, song)
        except Exception as exc:
            logger.error(f"LLM 点歌发送异常: {exc}")
            return self._llm_tool_result("error", _LLM_TERMINAL_ERROR)
        return self._llm_tool_result(
            "played" if result.success else "send_failed",
            result.message,
            song={
                "name": song.name,
                "artists": song.artists or song.author,
                "platform": player.platform.display_name,
            },
            mode=result.mode,
        )

    @filter.llm_tool(name="search_and_play_music")
    async def search_and_play_music(
        self, event: AstrMessageEvent, query: str = ""
    ) -> str:
        """用户想听某首歌或某位歌手的歌时，搜索歌曲；单个结果直接播放，多个结果发送候选列表。不要用它确认候选序号。

        Args:
            query(string): 仅从用户原话提取的歌曲名称或歌手名称。优先歌曲名称，其次歌手名称；不要添加“游戏原声带”等用户未提及的内容。例如“我想听鸣潮的歌”传入“鸣潮”，“来一首小小奇迹”传入“小小奇迹”。
        """
        try:
            query = str(query or "").strip()

            # LLM 工具不再接收 platform，由插件配置决定唯一的默认播放器。
            player = self.get_player(default=True)

            logger.debug(f"LLM 正在通过{player.platform.display_name}搜索歌曲：{query}")
            songs = await player.fetch_songs(
                keyword=query,
                limit=self.cfg.real_song_limit,
                extra=self.cfg.default_player_name,
            )

            if len(songs) == 1:
                return await self._send_song_for_llm(event, player, songs[0])

            selection = self.sender.create_selection_context(
                event,
                songs,
                player,
                notify_on_timeout=True,
                start_timeout=False,
            )
            selection_mode = await self.sender.send_song_selection(
                event=event,
                songs=songs,
                player=player,
                selection_id=selection.selection_id,
            )
            if selection_mode is None:
                self.sender.clear_selection_by_id(selection.selection_id)
                return self._llm_tool_result(
                    "selection_send_failed", _LLM_TERMINAL_ERROR
                )

            return self._llm_tool_result(
                "awaiting_selection",
                "终端找到了好多，你想听哪个呀",
                selection_id=selection.selection_id,
                count=len(songs),
                expires_in=self.cfg.timeout,
                display_mode=selection_mode,
            )
        except Exception as exc:
            logger.error(f"LLM 搜索/播放流程异常: {exc}")
            return self._llm_tool_result("search_failed", _LLM_TERMINAL_ERROR)

    @filter.llm_tool(name="confirm_music_selection")
    async def confirm_music_selection(
        self,
        event: AstrMessageEvent,
        index: int = 0,
        selection_id: str = "",
    ) -> str:
        """用户已经收到音乐候选列表并回复序号时，确认该序号并播放对应歌曲。没有候选列表时不要调用。

        Args:
            index(number): 用户确认的候选歌曲序号，从1开始。如果用户传入的不是数字，需要根据用户的输入解析成整数数字。例如“第一首”传入1，“最后一首”传入selection.songs长度的最大值。
            selection_id(string): 可选候选列表标识；通常留空并使用当前会话最新列表
        """
        try:
            parsed_index = int(index)

            status, selection = self.sender.claim_selection(
                event,
                parsed_index,
                str(selection_id or "").strip() or None,
            )

            song = selection.songs[parsed_index - 1]
            return await self._send_song_for_llm(
                event,
                selection.player,
                song,
            )
        except Exception as exc:
            logger.error(f"LLM 确认/播放流程异常: {exc}")
            return self._llm_tool_result("error", _LLM_TERMINAL_ERROR)
