from html import escape
from aiofiles.os import remove, path as aiopath
from os import path as ospath

from .. import LOGGER, DOWNLOAD_DIR
from ..helper.ext_utils.bot_utils import cmd_exec, sync_to_async
from ..helper.ext_utils.links_utils import is_url, is_telegram_link
from ..helper.ext_utils.telegraph_helper import telegraph
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import send_message, get_tg_link_message


async def get_mediainfo(client, message):
    text = message.text.split("\n")[0].split(" ", 1)
    link = text[1].strip() if len(text) > 1 else ""
    reply_to = message.reply_to_message
    file_path = None
    msg = None

    if not link and reply_to:
        if reply_to.text:
            link = reply_to.text.split("\n", 1)[0].strip()

    if is_telegram_link(link):
        try:
            reply_to, _ = await get_tg_link_message(link)
            link = ""
        except Exception as e:
            await send_message(message, f"ERROR: {e}")
            return

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
            msg = await send_message(message, "<i>Downloading media...</i>")
            file_path = await reply_to.download(file_name=f"{DOWNLOAD_DIR}temp_mediainfo_{message.id}/")

    if not file_path and link:
        if is_url(link) or await aiopath.exists(link):
            file_path = link
        else:
            await send_message(message, "Invalid URL or path!")
            return

    if not file_path:
        await send_message(message, "Send a direct link/URL or reply to a media file with command!")
        return

    if not msg:
        msg = await send_message(message, "<i>Fetching MediaInfo...</i>")

    stdout, stderr, code = await cmd_exec(["mediainfo", file_path])

    if file_path and not is_url(file_path) and not await aiopath.isfile(file_path):
        # if folder download or temporary download path
        pass

    if file_path and f"{DOWNLOAD_DIR}temp_mediainfo_{message.id}/" in file_path:
        dir_path = ospath.dirname(file_path)
        if await aiopath.exists(file_path):
            await remove(file_path)
        if await aiopath.exists(dir_path):
            try:
                from aioshutil import rmtree
                await rmtree(dir_path, ignore_errors=True)
            except Exception as e:
                LOGGER.error(f"MediaInfo cleanup error: {e}")

    if code != 0 or not stdout:
        err = stderr if stderr else "Failed to fetch MediaInfo!"
        await send_message(message, f"<b>MediaInfo Error:</b> <code>{escape(err)}</code>")
        if msg:
            await msg.delete()
        return

    title = "MediaInfo"
    if reply_to and hasattr(reply_to, "caption") and reply_to.caption:
        title = reply_to.caption.split("\n")[0][:30]
    elif file_path:
        title = ospath.basename(file_path)[:30]

    html_content = f"<pre>{escape(stdout)}</pre>"
    try:
        page = await telegraph.create_page(title=f"MediaInfo - {title}", content=html_content)
        telegraph_url = f"https://telegra.ph/{page['path']}"
        buttons = ButtonMaker()
        buttons.url_button("ℹ️ View MediaInfo", telegraph_url)
        await send_message(message, f"<b>MediaInfo generated successfully!</b>", buttons.build_menu(1))
    except Exception as e:
        LOGGER.error(f"MediaInfo Telegraph error: {e}")
        await send_message(message, f"<b>Telegraph Error:</b> <code>{escape(str(e))}</code>")
    finally:
        if msg:
            await msg.delete()
