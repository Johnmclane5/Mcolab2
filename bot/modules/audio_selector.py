from json import loads as json_loads
from os import path as ospath
from time import time
from asyncio import wait_for, Event
from functools import partial
from pyrogram.filters import regex, user
from pyrogram.handlers import CallbackQueryHandler
from aiofiles import open as aiopen
from aiofiles.os import remove, makedirs, path as aiopath
from aioshutil import rmtree

from .. import LOGGER, DOWNLOAD_DIR
from ..helper.ext_utils.bot_utils import cmd_exec, new_task, sync_to_async
from ..helper.ext_utils.links_utils import is_url, is_telegram_link
from ..helper.ext_utils.status_utils import get_readable_time
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    send_message,
    edit_message,
    delete_message,
    get_tg_link_message,
)


async def _download_header_bytes(tg_client, reply_to, file_path, limit_mb=20):
    await makedirs(ospath.dirname(file_path), exist_ok=True)
    downloaded = 0
    limit_bytes = limit_mb * 1024 * 1024
    try:
        async with aiopen(file_path, "wb") as f:
            async for chunk in tg_client.stream_media(reply_to):
                await f.write(chunk)
                downloaded += len(chunk)
                if downloaded >= limit_bytes:
                    break
        return True
    except Exception as e:
        LOGGER.error(f"Error streaming header bytes for audio selection: {e}")
        return False


async def probe_audio_streams(listener, link, reply_to=None, session=""):
    file_path = None
    temp_dir = f"{DOWNLOAD_DIR}temp_daudio_{listener.mid}"

    if is_telegram_link(link):
        try:
            reply_to, session = await get_tg_link_message(link)
            link = ""
        except Exception as e:
            LOGGER.error(f"Error getting telegram link message: {e}")

    if reply_to and not link:
        file_ = (
            reply_to.document
            or reply_to.photo
            or reply_to.video
            or reply_to.audio
            or reply_to.voice
            or reply_to.video_note
            or reply_to.sticker
            or reply_to.animation
            or None
        )
        if file_:
            temp_path = f"{temp_dir}/temp_header.media"
            if session == "user":
                from ..core.telegram_manager import TgClient
                reply_to_msg = await TgClient.user.get_messages(
                    chat_id=reply_to.chat.id, message_ids=reply_to.id
                )
                success = await _download_header_bytes(TgClient.user, reply_to_msg, temp_path, limit_mb=20)
            else:
                success = await _download_header_bytes(listener.client, reply_to, temp_path, limit_mb=20)
            if success:
                file_path = temp_path

    if not file_path and link:
        if is_url(link) or await aiopath.exists(link):
            file_path = link

    if not file_path:
        return []

    try:
        cmd = [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            file_path,
        ]
        stdout, stderr, code = await cmd_exec(cmd)
        if code != 0 or not stdout:
            LOGGER.error(f"probe_audio_streams ffprobe failed: {stderr}")
            return []
        streams = json_loads(stdout).get("streams", [])
        audio_streams = []
        audio_idx = 0
        for s in streams:
            if s.get("codec_type") == "audio":
                tags = s.get("tags", {})
                lang = (
                    tags.get("language")
                    or tags.get("LANGUAGE")
                    or tags.get("title")
                    or tags.get("TITLE")
                    or f"Track {audio_idx}"
                )
                codec = s.get("codec_name", "unknown")
                channels = s.get("channels", 2)
                audio_streams.append({
                    "index": audio_idx,
                    "lang": lang,
                    "codec": codec,
                    "channels": channels,
                })
                audio_idx += 1
        return audio_streams
    except Exception as e:
        LOGGER.error(f"Error probing audio streams: {e}")
        return []
    finally:
        if await aiopath.exists(temp_dir):
            try:
                await rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                LOGGER.error(f"Cleanup error in daudio temp dir: {e}")


@new_task
async def select_audio_handler(_, query, obj):
    data = query.data.split()
    message = query.message
    await query.answer()

    if data[1] == "cancel":
        await edit_message(message, "Audio selection cancelled. Task cancelled.")
        obj.selected_audio = None
        obj.listener.is_cancelled = True
        obj.event.set()
    elif data[1] == "select":
        obj.selected_audio = data[2]
        obj.event.set()


class AudioSelection:
    def __init__(self, listener):
        self.listener = listener
        self.event = Event()
        self.reply_to = None
        self._time = time()
        self._timeout = 120
        self.selected_audio = None

    async def get_audio_choice(self, audio_streams):
        if not audio_streams or len(audio_streams) < 2:
            LOGGER.warning("Not enough audio streams found for swapping via buttons.")
            return None

        buttons = ButtonMaker()
        for s in audio_streams[1:]:
            idx = s["index"]
            btn_text = f"Swap Track 0 ↔ Track {idx}: {s['lang']} ({s['codec']}, {s['channels']}ch)"
            buttons.data_button(btn_text, f"daudio select {idx}")
        buttons.data_button("Cancel", "daudio cancel", "footer")

        menu = buttons.build_menu(1)
        msg = (
            f"<b>Select Audio Track to Swap with Default Track 0:</b>\n"
            f"Track 0: {audio_streams[0]['lang']} ({audio_streams[0]['codec']}, {audio_streams[0]['channels']}ch)\n\n"
            f"Timeout: {get_readable_time(self._timeout - (time() - self._time))}"
        )
        self.reply_to = await send_message(self.listener.message, msg, menu)

        pfunc = partial(select_audio_handler, obj=self)
        handler = self.listener.client.add_handler(
            CallbackQueryHandler(
                pfunc, filters=regex("^daudio") & user(self.listener.user_id)
            ),
            group=-1,
        )
        try:
            await wait_for(self.event.wait(), timeout=self._timeout)
        except Exception:
            await edit_message(self.reply_to, "Timed Out. Audio selection cancelled!")
            self.selected_audio = None
            self.listener.is_cancelled = True
            self.event.set()
        finally:
            self.listener.client.remove_handler(*handler)

        if not self.listener.is_cancelled and self.reply_to:
            await delete_message(self.reply_to)

        return self.selected_audio
