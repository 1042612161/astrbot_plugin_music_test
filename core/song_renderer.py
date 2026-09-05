import asyncio
import html
import unicodedata
from io import BytesIO
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

from astrbot import logger

from .config import PluginConfig
from .model import Song

try:
    from pytakumi import Renderer as TakumiRenderer
    from pytakumi import html_to_pic as takumi_html_to_pic
except ImportError:  # Optional at runtime; Pillow remains the safe fallback.
    TakumiRenderer = None  # type: ignore[assignment,misc]
    takumi_html_to_pic = None  # type: ignore[assignment]


TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "search_list.html"
PLACEHOLDER_COVER_URL = (
    "https://backend.appmiaoda.com/projects/supabase316894448002838528/"
    "storage/v1/object/public/images/d3924190-3923-4fb6-ad91-fcfa42d43c4f.jpg"
)
_VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}
_EMOJI_MODIFIERS = set(chr(code) for code in range(0x1F3FB, 0x1F400))


class CardTheme:
    canvas_width: int = 680
    card_width: int = 220
    card_height: int = 278
    thumb_height: int = 220
    margin: int = 16
    corner_radius: int = 10
    font_size: int = 16
    card_bg: str = "#ffffff"
    canvas_bg: str = "#f5f5f5"
    title_color: str = "#000000"
    sub_text_color: str = "#666666"
    overlay_text_color: str = "#ffffff"
    gradient_height: int = 40
    gradient_max_alpha: int = 180

    def load_font(self, font_path: str) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(font_path, self.font_size)


class CardRenderer:
    def __init__(
        self,
        config: PluginConfig,
        theme: CardTheme | None = None,
    ):
        self.cfg = config
        self.theme = theme or CardTheme()
        self.font_role_paths: dict[str, Path] = dict(
            getattr(config, "font_role_paths", {"primary": Path(config.font_path)})
        )
        self.pillow_fonts = self._load_pillow_fonts()
        self.font = self.pillow_fonts["primary"]
        self._takumi = None
        self._takumi_families: list[str] = []
        self._ensure_takumi()

    def _ensure_takumi(self) -> None:
        if self._takumi is not None or TakumiRenderer is None:
            return
        try:
            # Image resources are supplied per render and should not survive
            # a sent message; the renderer cache is limited to font/layout data.
            self._takumi = TakumiRenderer(cache_max_bytes=0)
            registered_paths: set[Path] = set()
            for font_path in self.font_role_paths.values():
                resolved_path = Path(font_path).resolve()
                if resolved_path in registered_paths:
                    continue
                registered_paths.add(resolved_path)
                families = self._takumi.register_font(
                    resolved_path.read_bytes(),
                    name=resolved_path.stem,
                )
                self._takumi_families.extend(str(item["name"]) for item in families)
        except Exception as exc:
            logger.warning(f"pytakumi 初始化失败，将回退 Pillow 渲染: {exc}")
            self._takumi = None
            self._takumi_families = []

    def clear_cache(self) -> None:
        """Drop Takumi's renderer/cache when the plugin is unloaded."""
        self._takumi = None
        self._takumi_families = []

    def _load_pillow_fonts(self) -> dict[str, ImageFont.FreeTypeFont]:
        fonts: dict[str, ImageFont.FreeTypeFont] = {}
        for role, path in self.font_role_paths.items():
            try:
                font = self.theme.load_font(str(path))
            except Exception as exc:
                if role != "emoji":
                    logger.warning(f"Pillow 加载 {role} 字体失败 {path}: {exc}")
                    continue
                font = self._load_bitmap_font(path)
                if font is None:
                    logger.warning(f"Pillow 加载 emoji 字体失败 {path}: {exc}")
                    continue
            fonts[role] = font
        if "primary" not in fonts:
            raise RuntimeError("Pillow 无法加载配置的主渲染字体")
        return fonts

    def _load_bitmap_font(self, path: Path) -> ImageFont.FreeTypeFont | None:
        """Find a fixed bitmap strike used by color emoji fonts."""
        for size in range(self.theme.font_size + 1, 257):
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
        return None

    @staticmethod
    def _font_has_glyph(font: ImageFont.FreeTypeFont, text: str) -> bool:
        """Best-effort glyph detection using Pillow's replacement glyph."""
        if not text:
            return True
        try:
            mask = font.getmask(text)
            missing = font.getmask("\U0010ffff")
            return not (mask.size == missing.size and bytes(mask) == bytes(missing))
        except Exception:
            return False

    @staticmethod
    def _text_clusters(text: str) -> list[str]:
        """Keep combining marks, flags, keycaps and ZWJ emoji together."""
        clusters: list[str] = []
        for char in text:
            codepoint = ord(char)
            if clusters and (
                char in _VARIATION_SELECTORS
                or char in _EMOJI_MODIFIERS
                or char == "\u200d"
                or clusters[-1].endswith("\u200d")
                or char == "\u20e3"
                or unicodedata.combining(char)
                or 0xE0020 <= codepoint <= 0xE007F
                or (
                    0x1F1E6 <= codepoint <= 0x1F1FF
                    and len(clusters[-1]) == 1
                    and 0x1F1E6 <= ord(clusters[-1]) <= 0x1F1FF
                )
            ):
                clusters[-1] += char
            else:
                clusters.append(char)
        return clusters

    def _font_for_cluster(
        self,
        cluster: str,
    ) -> tuple[str, ImageFont.FreeTypeFont] | None:
        roles = ("emoji", "primary", "other") if self._is_emoji_cluster(cluster) else (
            "primary",
            "other",
            "emoji",
        )
        for role in roles:
            font = self.pillow_fonts.get(role)
            if font is None:
                continue
            if self._font_has_glyph(font, cluster):
                return role, font
        return None

    @staticmethod
    def _is_emoji_cluster(cluster: str) -> bool:
        return any(
            0x1F000 <= ord(char) <= 0x1FAFF
            or 0x2600 <= ord(char) <= 0x27BF
            or char == "\u20e3"
            for char in cluster
        ) or "\ufe0f" in cluster

    def _draw_emoji(
        self,
        image: Image.Image,
        xy: tuple[int, int],
        cluster: str,
        font: ImageFont.FreeTypeFont,
        line_height: int,
    ) -> int:
        """Draw a color/bitmap emoji font and scale it to the text line."""
        bbox = font.getbbox(cluster)
        width = max(1, bbox[2] - bbox[0])
        height = max(1, bbox[3] - bbox[1])
        glyph = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        glyph_draw = ImageDraw.Draw(glyph)
        glyph_draw.text(
            (-bbox[0], -bbox[1]),
            cluster,
            font=font,
            embedded_color=True,
        )
        scale = line_height / height
        target_width = max(1, round(width * scale))
        glyph = glyph.resize((target_width, line_height), Image.Resampling.LANCZOS)
        image.alpha_composite(glyph, dest=(round(xy[0]), round(xy[1])))
        return target_width

    def _draw_text(
        self,
        image: Image.Image,
        xy: tuple[int, int],
        text: str,
        *,
        fill: str,
    ) -> None:
        """Draw text with per-cluster font fallback for Pillow rendering."""
        draw = ImageDraw.Draw(image)
        x, y = xy
        bbox = self.font.getbbox("Ag")
        line_height = max(1, bbox[3] - bbox[1])
        for line in str(text).split("\n"):
            cursor = x
            for cluster in self._text_clusters(line):
                selected = self._font_for_cluster(cluster)
                if selected is None:
                    role, font = "primary", self.font
                else:
                    role, font = selected
                if role == "emoji":
                    cursor += self._draw_emoji(
                        image,
                        (cursor, y),
                        cluster,
                        font,
                        line_height,
                    )
                else:
                    draw.text((cursor, y), cluster, font=font, fill=fill)
                    cursor += int(font.getlength(cluster))
            y += line_height

    def format_count(self, count: int) -> str:
        if count >= 10000:
            return f"{count / 10000:.1f}w"
        if count >= 1000:
            return f"{count / 1000:.1f}k"
        return str(count)

    async def draw_card(
        self,
        media: dict,
        index: int,
        cover_map: dict[str, Image.Image],
    ) -> Image.Image:
        try:
            theme = self.theme
            card = Image.new(
                "RGBA",
                (theme.card_width, theme.card_height),
                theme.card_bg,
            )
            draw = ImageDraw.Draw(card)

            raw_url = str(media.get("cover") or media.get("pic") or "")

            pic_url = raw_url
            thumb = cover_map.get(pic_url) or Image.new(
                "RGB",
                (theme.card_width, theme.thumb_height),
                "#e5e5e5",
            )
            thumb = thumb.resize((theme.card_width, theme.thumb_height))
            card.paste(thumb, (0, 0))

            alpha_gradient = Image.new(
                "L",
                (theme.card_width, theme.gradient_height),
                color=0,
            )
            gradient_draw = ImageDraw.Draw(alpha_gradient)
            for y in range(theme.gradient_height):
                alpha = int(theme.gradient_max_alpha * (y / theme.gradient_height))
                gradient_draw.line([(0, y), (theme.card_width, y)], fill=alpha)

            overlay = Image.new(
                "RGBA",
                (theme.card_width, theme.gradient_height),
                color=(0, 0, 0, 255),
            )
            overlay.putalpha(alpha_gradient)
            card.paste(
                overlay,
                (0, theme.thumb_height - theme.gradient_height),
                overlay,
            )

            self._draw_text(
                card,
                (8, theme.thumb_height - 20),
                self.format_count(int(media.get("play", 0) or 0)),
                fill=theme.overlay_text_color,
            )
            self._draw_text(
                card,
                (theme.card_width - 40, theme.thumb_height - 20),
                str(media.get("duration") or "0:00"),
                fill=theme.overlay_text_color,
            )

            raw_title = BeautifulSoup(
                str(media.get("title") or ""),
                "html.parser",
            ).get_text()
            title = (
                raw_title[:18] + "\n" + raw_title[18:36] + "..."
                if len(raw_title) > 36
                else raw_title[:18] + "\n" + raw_title[18:]
            )
            self._draw_text(
                card,
                (8, theme.thumb_height + 8),
                title,
                fill=theme.title_color,
            )

            self._draw_text(
                card,
                (8, theme.card_height - 30),
                self._build_author_text(media),
                fill=theme.sub_text_color,
            )

            self._draw_text(
                card,
                (theme.card_width - 20, theme.card_height - 25),
                str(index),
                fill=theme.sub_text_color,
            )

            mask = Image.new("L", (theme.card_width, theme.card_height), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.rounded_rectangle(
                (0, 0, theme.card_width, theme.card_height),
                radius=theme.corner_radius,
                fill=255,
            )
            card.putalpha(mask)
            return card

        except Exception as exc:
            logger.error(f"render card failed: {exc}")
            return Image.new(
                "RGBA",
                (self.theme.card_width, self.theme.card_height),
                self.theme.card_bg,
            )

    async def _render_list_image_pillow(
        self,
        media_list: list,
        cover_map: dict[str, Image.Image],
        jpeg_quality: int = 80,
    ) -> bytes:
        theme = self.theme
        tasks = [
            self.draw_card(media, index=i + 1, cover_map=cover_map)
            for i, media in enumerate(media_list)
        ]
        cards = await asyncio.gather(*tasks)

        rows: list[Image.Image] = []
        per_row = self.cfg.cards_per_row
        for i in range(0, len(cards), per_row):
            row_cards = cards[i : i + per_row]
            row_width = per_row * theme.card_width + (per_row + 1) * theme.margin
            row_img = Image.new(
                "RGBA",
                (row_width, theme.card_height + 2 * theme.margin),
                theme.canvas_bg,
            )
            for j, card in enumerate(row_cards):
                x = theme.margin + j * (theme.card_width + theme.margin)
                row_img.paste(card, (x, theme.margin), card)
            rows.append(row_img)

        total_width = rows[0].width
        total_height = sum(row.height for row in rows)

        canvas = Image.new(
            "RGBA",
            (total_width, total_height),
            theme.canvas_bg,
        )

        y_offset = 0
        for row in rows:
            canvas.paste(row, (0, y_offset), row)
            y_offset += row.height

        final_image = Image.new("RGB", canvas.size, theme.canvas_bg)
        final_image.paste(canvas, mask=canvas.split()[3])

        buffer = BytesIO()
        final_image.save(buffer, format="JPEG", quality=jpeg_quality)
        return buffer.getvalue()

    @staticmethod
    def _image_bytes(image: Image.Image | None) -> bytes | None:
        if image is None:
            return None
        buf = BytesIO()
        image.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()

    def _html_row(
        self,
        media: dict,
        index: int,
        cover_key: str,
    ) -> str:
        title = html.escape(str(media.get("title") or "未知曲目"), quote=True)
        artist = html.escape(str(media.get("author") or "未知歌手"), quote=True)
        album = html.escape(str(media.get("album") or "单曲"), quote=True)
        duration = html.escape(str(media.get("duration") or "--:--"), quote=True)
        source = html.escape(str(media.get("source") or "MUSIC").upper(), quote=True)
        return (
            '<article class="song-row">'
            f'<span class="index">{index}</span>'
            f'<img class="cover" src="{cover_key}" alt="" />'
            '<div class="song-info">'
            f'<div class="song-name">{title}</div>'
            f'<div class="song-artist">{artist}</div>'
            f'<div class="song-album">{album}</div>'
            '</div>'
            f'<span class="duration">{duration}</span>'
            f'<span class="source">{source}</span>'
            '</article>'
        )

    async def _render_list_image_takumi(
        self,
        media_list: list,
        cover_map: dict[str, Image.Image],
        *,
        title: str,
        hint: str,
        jpeg_quality: int,
    ) -> bytes:
        if self._takumi is None:
            raise RuntimeError("pytakumi unavailable")
        theme = self.theme
        rows_height = max(1, len(media_list)) * 82 + max(0, len(media_list) - 1) * 9
        height = 263 + rows_height
        images: dict[str, bytes] = {}
        row_html: list[str] = []
        placeholder_key = "memory://music-cover-placeholder"
        placeholder = Image.new("RGB", (160, 160), "#ffd6e7")
        ImageDraw.Draw(placeholder).text(
            (80, 80), "♪", anchor="mm", fill="#ff6b9a", font=self.font
        )
        images[placeholder_key] = self._image_bytes(placeholder) or b""
        hero_key = "memory://music-cover-hero"
        first_url = str(media_list[0].get("cover") or media_list[0].get("pic") or "") if media_list else ""
        placeholder_image = cover_map.get(PLACEHOLDER_COVER_URL)
        hero_bytes = self._image_bytes(cover_map.get(first_url) or placeholder_image)
        if hero_bytes:
            images[hero_key] = hero_bytes
        else:
            hero_key = placeholder_key
        for index, media in enumerate(media_list, 1):
            key = f"memory://music-cover-{index}"
            raw_url = str(media.get("cover") or media.get("pic") or PLACEHOLDER_COVER_URL)
            cover_bytes = self._image_bytes(cover_map.get(raw_url))
            if cover_bytes:
                images[key] = cover_bytes
            else:
                cover_bytes = self._image_bytes(placeholder_image)
                if cover_bytes:
                    images[placeholder_key] = cover_bytes
                key = placeholder_key
            row_html.append(self._html_row(media, index, key))
        html_text = TEMPLATE_PATH.read_text(encoding="utf-8")
        html_text = html_text.replace("{{HEIGHT}}", str(height))
        font_family = ", ".join(
            f'"{name.replace(chr(34), "")}"' for name in self._takumi_families
        )
        html_text = html_text.replace(
            "{{FONT_FAMILY}}", f"{font_family}, sans-serif" if font_family else "sans-serif"
        )
        html_text = html_text.replace("{{TITLE}}", html.escape(str(title), quote=True))
        html_text = html_text.replace("{{HINT}}", html.escape(str(hint), quote=True))
        html_text = html_text.replace("{{HERO_COVER}}", f'<img class="record-cover" src="{hero_key}" alt="" />')
        html_text = html_text.replace("{{ROWS}}", "".join(row_html))
        if takumi_html_to_pic is None:
            raise RuntimeError("pytakumi html helper unavailable")
        return await asyncio.to_thread(
            takumi_html_to_pic,
            html_text,
            width=theme.canvas_width,
            height=height,
            format="jpeg",
            quality=jpeg_quality,
            images=images,
            renderer=self._takumi,
            font_families=self._takumi_families,
            lang="zh-CN",
        )

    async def render_list_image(
        self,
        media_list: list,
        cover_map: dict[str, Image.Image],
        jpeg_quality: int = 88,
        *,
        title: str = "音乐点歌候选",
        hint: str | None = None,
    ) -> bytes:
        hint = hint or f"回复数字 1～{len(media_list)} 播放对应曲目"
        self._ensure_takumi()
        try:
            return await self._render_list_image_takumi(
                media_list,
                cover_map,
                title=title,
                hint=hint,
                jpeg_quality=jpeg_quality,
            )
        except Exception as exc:
            logger.warning(f"pytakumi 卡片渲染失败，回退 Pillow: {exc}")
            return await self._render_list_image_pillow(media_list, cover_map, jpeg_quality)

    async def render_song_list_image(
        self,
        songs: list[Song],
        cover_map: dict[str, Image.Image],
        jpeg_quality: int = 80,
        *,
        title: str = "音乐点歌候选",
        hint: str | None = None,
        source_label: str | None = None,
    ) -> bytes:
        media_list = [
            {
                "cover": song.cover_url,
                "title": song.name or song.title or "未知曲目",
                "author": song.artists or song.author or "未知歌手",
                "album": "单曲",
                "duration": self._format_duration(song.duration),
                "play": 0,
                "source": source_label or song.source or "MUSIC",
            }
            for song in songs
        ]
        return await self.render_list_image(
            media_list,
            cover_map,
            jpeg_quality=jpeg_quality,
            title=title,
            hint=hint,
        )

    @staticmethod
    def _build_author_text(media: dict) -> str:
        return str(media.get("author") or "").strip() or "-"

    @staticmethod
    def _format_duration(duration_ms: int | None) -> str:
        if not duration_ms:
            return "0:00"
        duration = duration_ms // 1000
        minutes = duration // 60
        seconds = duration % 60
        return f"{minutes}:{seconds:02d}"
