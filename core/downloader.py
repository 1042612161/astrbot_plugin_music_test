import uuid
from pathlib import Path
from typing import Final

import aiofiles
import aiohttp

from astrbot.api import logger

from .config import PluginConfig


MAX_AUDIO_LINK_BYTES: Final = 15 * 1024 * 1024


class Downloader:
    """下载器"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.songs_dir = self.cfg.songs_dir
        self._cleanup_song_files()
        self.session = aiohttp.ClientSession(proxy=self.cfg.http_proxy)

    async def close(self):
        await self.session.close()

    def _cleanup_song_files(self) -> None:
        """Remove audio files left by a previous plugin process."""
        try:
            self.songs_dir.mkdir(parents=True, exist_ok=True)
            removed = 0
            for path in self.songs_dir.iterdir():
                if not path.is_file():
                    continue
                # Only remove files created by this downloader. Other files in
                # the temporary directory must not be touched.
                if not path.name.lower().endswith((".mp3", ".mp3.part")):
                    continue
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning(f"清理历史歌曲文件失败 {path}: {exc}")
            if removed:
                logger.info(f"插件启动时清理历史歌曲文件 {removed} 个")
        except OSError as exc:
            logger.warning(f"初始化歌曲临时目录失败，无法清理历史文件: {exc}")

    def remove_song_file(self, file_path: Path) -> None:
        """Remove a downloaded audio file without affecting send flow."""
        try:
            file_path.unlink(missing_ok=True)
        except OSError as exc:
            # Do not turn a successful platform send into a failed command if
            # the platform or antivirus still has the file open.
            logger.warning(f"发送后清理歌曲文件失败 {file_path}: {exc}")

    async def download_image(self, url: str, close_ssl: bool = True) -> bytes | None:
        """下载图片"""
        url = url.replace("https://", "http://") if close_ssl else url
        try:
            async with self.session.get(url) as response:
                img_bytes = await response.read()
                return img_bytes
        except Exception as e:
            logger.error(f"图片下载失败: {e}")

    async def audio_link_within_limit(
        self,
        url: str,
        max_bytes: int = MAX_AUDIO_LINK_BYTES,
    ) -> bool:
        """Check a remote audio URL without downloading or re-encoding it.

        Most music APIs provide ``Content-Length``.  When an endpoint omits it
        or rejects HEAD, the URL is allowed so that a metadata quirk does not
        prevent sending; NapCat still fetches the original compressed stream.
        """
        try:
            async with self.session.head(
                url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                length = response.headers.get("Content-Length")
                if not length:
                    return True
                try:
                    return int(length) <= max_bytes
                except ValueError:
                    return True
        except Exception as exc:
            logger.debug(f"检查音频链接大小失败，继续使用原始链接发送: {exc}")
            return True

    async def download_song(self, url: str) -> Path | None:
        """下载歌曲，返回保存路径"""
        self.songs_dir.mkdir(parents=True, exist_ok=True)
        song_uuid = uuid.uuid4().hex
        file_path = self.songs_dir / f"{song_uuid}.mp3"
        part_path = self.songs_dir / f"{song_uuid}.mp3.part"
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    logger.error(f"歌曲下载失败，HTTP 状态码：{response.status}")
                    return None
                # 流式写入
                async with aiofiles.open(part_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(1024):
                        await f.write(chunk)

            # Rename only after the complete response has been written, so
            # cleanup tasks and senders never see a partially downloaded .mp3.
            part_path.replace(file_path)

            logger.debug(f"歌曲下载完成，保存在：{file_path}")
            return file_path

        except Exception as e:
            try:
                part_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                logger.warning(f"清理未完成歌曲文件失败 {part_path}: {cleanup_error}")
            logger.error(f"歌曲下载失败，错误信息：{e}")
            return None
