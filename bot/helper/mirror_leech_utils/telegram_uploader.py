import contextlib
import aiohttp
import re
import PTN
import imgbbpy
from PIL import Image
from aioshutil import rmtree
from asyncio import sleep
from logging import getLogger
from natsort import natsorted
from os import walk, path as ospath
from time import time
from re import match as re_match, sub as re_sub
from pyrogram.errors import FloodWait, RPCError, FloodPremiumWait, BadRequest
from aiofiles.os import (
    remove,
    path as aiopath,
    rename, mkdir,
)
from pyrogram.types import (
    InputMediaVideo,
    InputMediaDocument,
    InputMediaPhoto,
)
from tenacity import (
    retry,
    wait_exponential,
    stop_after_attempt,
    retry_if_exception_type,
    RetryError,
)

from ...core.config_manager import Config
from ...core.telegram_manager import TgClient
from ..ext_utils.bot_utils import sync_to_async
from ..ext_utils.files_utils import is_archive, get_base_name
from ..telegram_helper.message_utils import delete_message
from ..ext_utils.media_utils import (
    get_media_info,
    get_document_type,
    get_video_thumbnail,
    get_audio_thumbnail,
    get_multiple_frames_thumbnail,
)
from ..ext_utils.extras import remove_extension, remove_redandent, get_movie_poster, get_tv_poster, extract_file_info
from motor.motor_asyncio import AsyncIOMotorClient 
from ..ext_utils.bot_utils import sync_to_async, download_image_url

LOGGER = getLogger(__name__)

try:
    mongo_client = AsyncIOMotorClient(Config.IBB_URL)  # Use AsyncIOMotorClient
    db = mongo_client['sharing_bot']
    files_col = db['files']
except Exception as e:
    LOGGER.error(f"Failed to connect to MongoDB: {e}")
    db = None
    pass

class TelegramUploader:
    def __init__(self, listener, path):
        self._last_uploaded = 0
        self._processed_bytes = 0
        self._listener = listener
        self._path = path
        self._start_time = time()
        self._total_files = 0
        self._thumb = self._listener.thumb or f"thumbnails/{listener.user_id}.jpg"
        self._msgs_dict = {}
        self._corrupted = 0
        self._is_corrupted = False
        self._media_dict = {"videos": {}, "documents": {}}
        self._last_msg_in_group = False
        self._up_path = ""
        self._lprefix = ""
        self._media_group = False
        self._is_private = False
        self._sent_msg = None
        self._user_session = self._listener.user_transmission
        self._error = ""
        self._user_dump = ""

    async def get_custom_thumb(self, thumb):
        photo_dir = await download_image_url(thumb)
    
        if await aiopath.exists(photo_dir):
            path = "Thumbnails"
            if not await aiopath.isdir(path):
                await mkdir(path)
            des_dir = ospath.join(path, f'{time()}.jpg')
            await sync_to_async(Image.open(photo_dir).convert("RGB").save, des_dir, "JPEG")
            await remove(photo_dir)
            return des_dir
        return None     

    async def _upload_progress(self, current, _):
        if self._listener.is_cancelled:
            if self._user_session:
                TgClient.user.stop_transmission()
            else:
                self._listener.client.stop_transmission()
        chunk_size = current - self._last_uploaded
        self._last_uploaded = current
        self._processed_bytes += chunk_size

    async def _user_settings(self):
        self._media_group = self._listener.user_dict.get("MEDIA_GROUP") or (
            Config.MEDIA_GROUP
            if "MEDIA_GROUP" not in self._listener.user_dict
            else False
        )
        self._lprefix = self._listener.user_dict.get("LEECH_FILENAME_PREFIX") or (
            Config.LEECH_FILENAME_PREFIX
            if "LEECH_FILENAME_PREFIX" not in self._listener.user_dict
            else ""
        )
        if self._listener.user_dump:
            self._user_dump = self._listener.user_dict.get("USER_DUMP", {}).get(self._listener.user_dump)
        else:
            self._user_dump = self._listener.user_dict.get("ACTIVE_USER_DUMP")
        if self._thumb != "none" and not await aiopath.exists(self._thumb):
            self._thumb = None

    async def _msg_to_reply(self):
        if self._listener.up_dest:
            msg = (
                self._listener.message.link
                if self._listener.is_super_chat
                else self._listener.message.text.lstrip("/")
            )
            try:
                if self._user_session:
                    self._sent_msg = await TgClient.user.send_message(
                        chat_id=self._listener.up_dest,
                        text=msg,
                        message_thread_id=self._listener.chat_thread_id,
                        disable_notification=True,
                    )
                else:
                    self._sent_msg = await self._listener.client.send_message(
                        chat_id=self._listener.up_dest,
                        text=msg,
                        message_thread_id=self._listener.chat_thread_id,
                        disable_notification=True,
                    )
                    self._is_private = self._sent_msg.chat.type.name == "PRIVATE"
            except Exception as e:
                await self._listener.on_upload_error(str(e))
                return False
        elif self._user_session:
            self._sent_msg = await TgClient.user.get_messages(
                chat_id=self._listener.message.chat.id, message_ids=self._listener.mid
            )
            if self._sent_msg is None:
                self._sent_msg = await TgClient.user.send_message(
                    chat_id=self._listener.message.chat.id,
                    text="Deleted Cmd Message! Don't delete the cmd message again!",
                    disable_notification=True,
                )
        else:
            self._sent_msg = self._listener.message
        return True

    async def _prepare_file(self, file_, dirpath):
        if self._lprefix:
            cap_mono = f"{self._lprefix} <code>{file_}</code>"
            self._lprefix = re_sub("<.*?>", "", self._lprefix)
            new_path = ospath.join(dirpath, f"{self._lprefix} {file_}")
            await rename(self._up_path, new_path)
            self._up_path = new_path
        else:
            cap_mono = f"<code>{file_}</code>"
        if len(file_) > 60:
            if is_archive(file_):
                name = get_base_name(file_)
                ext = file_.split(name, 1)[1]
            elif match := re_match(r".+(?=\..+\.0*\d+$)|.+(?=\.part\d+\..+$)", file_):
                name = match.group(0)
                ext = file_.split(name, 1)[1]
            elif len(fsplit := ospath.splitext(file_)) > 1:
                name = fsplit[0]
                ext = fsplit[1]
            else:
                name = file_
                ext = ""
            extn = len(ext)
            remain = 60 - extn
            name = name[:remain]
            new_path = ospath.join(dirpath, f"{name}{ext}")
            await rename(self._up_path, new_path)
            self._up_path = new_path
        return cap_mono

    def _get_input_media(self, subkey, key):
        rlist = []
        for msg in self._media_dict[key][subkey]:
            if key == "videos":
                input_media = InputMediaVideo(
                    media=msg.video.file_id, caption=msg.caption
                )
            else:
                input_media = InputMediaDocument(
                    media=msg.document.file_id, caption=msg.caption
                )
            rlist.append(input_media)
        return rlist

    async def _send_screenshots(self, dirpath, outputs):
        inputs = [
            InputMediaPhoto(ospath.join(dirpath, p), p.rsplit("/", 1)[-1])
            for p in outputs
        ]
        for i in range(0, len(inputs), 10):
            batch = inputs[i : i + 10]
            self._sent_msg = (
                await self._sent_msg.reply_media_group(
                    media=batch,
                    quote=True,
                    disable_notification=True,
                )
            )[-1]

    async def _send_media_group(self, subkey, key, msgs):
        for index, msg in enumerate(msgs):
            if self._listener.hybrid_leech or not self._user_session:
                msgs[index] = await self._listener.client.get_messages(
                    chat_id=msg[0], message_ids=msg[1]
                )
            else:
                msgs[index] = await TgClient.user.get_messages(
                    chat_id=msg[0], message_ids=msg[1]
                )
        msgs_list = await msgs[0].reply_to_message.reply_media_group(
            media=self._get_input_media(subkey, key),
            quote=True,
            disable_notification=True,
        )
        for msg in msgs:
            if msg.link in self._msgs_dict:
                del self._msgs_dict[msg.link]
            await delete_message(msg)
        del self._media_dict[key][subkey]
        if self._listener.is_super_chat or self._listener.up_dest:
            for m in msgs_list:
                self._msgs_dict[m.link] = m.caption
        self._sent_msg = msgs_list[-1]

    async def upload(self):
        await self._user_settings()
        res = await self._msg_to_reply()
        if not res:
            return
        for dirpath, _, files in natsorted(await sync_to_async(walk, self._path)):
            if dirpath.strip().endswith("/yt-dlp-thumb"):
                continue
            if dirpath.strip().endswith("_mltbss"):
                await self._send_screenshots(dirpath, files)
                await rmtree(dirpath, ignore_errors=True)
                continue
            for file_ in natsorted(files):
                self._error = ""
                self._up_path = f_path = ospath.join(dirpath, file_)
                if not await aiopath.exists(self._up_path):
                    LOGGER.error(f"{self._up_path} not exists! Continue uploading!")
                    continue

                # --- Check if file name exists in DB ---
                if db is not None:
                    no_ext = await remove_extension(file_)
                    existing = await files_col.find_one({"file_name": no_ext})
                    if existing:
                        LOGGER.info(
                            f"File '{file_}' already exists in DB. Proceeding with imgbb upload."
                        )
                        if 'poster_delete_url' in existing:
                            poster_url = existing['poster_delete_url']
                            await self._sent_msg.reply_text(
                                f"An old ImgBB poster for this file was found. Please delete it manually: {poster_url}",
                                disable_web_page_preview=True,
                            )
                        if self._listener.user_dict.get("IMGBB_UPLOAD") and self._listener.thumbnail_layout:
                            imgbb_thumb = await get_multiple_frames_thumbnail(
                                self._up_path,
                                self._listener.thumbnail_layout,
                                self._listener.screen_shots,
                            )
                        else:
                            imgbb_thumb = await get_video_thumbnail(self._up_path, None)
                        await self._upload_to_imgbb(imgbb_thumb, file_, existing)
                        await self.cancel_task()
                        return
                # --- End check ---

                try:
                    f_size = await aiopath.getsize(self._up_path)
                    self._total_files += 1
                    if f_size == 0:
                        LOGGER.error(
                            f"{self._up_path} size is zero, telegram don't upload zero size files"
                        )
                        self._corrupted += 1
                        continue
                    if self._listener.is_cancelled:
                        return
                    cap_mono = await self._prepare_file(file_, dirpath)
                    if self._last_msg_in_group:
                        group_lists = [
                            x for v in self._media_dict.values() for x in v.keys()
                        ]
                        match = re_match(r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)", f_path)
                        if not match or match and match.group(0) not in group_lists:
                            for key, value in list(self._media_dict.items()):
                                for subkey, msgs in list(value.items()):
                                    if len(msgs) > 1:
                                        await self._send_media_group(subkey, key, msgs)
                    if self._listener.hybrid_leech and self._listener.user_transmission:
                        self._user_session = f_size > 2097152000
                        if self._user_session:
                            self._sent_msg = await TgClient.user.get_messages(
                                chat_id=self._sent_msg.chat.id,
                                message_ids=self._sent_msg.id,
                            )
                        else:
                            self._sent_msg = await self._listener.client.get_messages(
                                chat_id=self._sent_msg.chat.id,
                                message_ids=self._sent_msg.id,
                            )
                    self._last_msg_in_group = False
                    self._last_uploaded = 0
                    await self._upload_file(cap_mono, file_, f_path)
                    if self._listener.is_cancelled:
                        return
                    if (
                        not self._is_corrupted
                        and (self._listener.is_super_chat or self._listener.up_dest)
                        and not self._is_private
                    ):
                        self._msgs_dict[self._sent_msg.link] = file_
                    await sleep(1)
                except Exception as err:
                    if isinstance(err, RetryError):
                        LOGGER.info(
                            f"Total Attempts: {err.last_attempt.attempt_number}"
                        )
                        err = err.last_attempt.exception()
                    LOGGER.error(f"{err}. Path: {self._up_path}")
                    self._error = str(err)
                    self._corrupted += 1
                    if self._listener.is_cancelled:
                        return
                if not self._listener.is_cancelled and await aiopath.exists(
                    self._up_path
                ):
                    await remove(self._up_path)
        for key, value in list(self._media_dict.items()):
            for subkey, msgs in list(value.items()):
                if len(msgs) > 1:
                    try:
                        await self._send_media_group(subkey, key, msgs)
                    except Exception as e:
                        LOGGER.info(
                            f"While sending media group at the end of task. Error: {e}"
                        )
        if self._listener.is_cancelled:
            return
        if self._total_files == 0:
            await self._listener.on_upload_error(
                "No files to upload. In case you have filled EXCLUDED/INCLUDED EXTENSIONS, then check if all files have those extensions or not."
            )
            return
        if self._total_files <= self._corrupted:
            await self._listener.on_upload_error(
                f"Files Corrupted or unable to upload. {self._error or 'Check logs!'}"
            )
            return
        LOGGER.info(f"Leech Completed: {self._listener.name}")
        await self._listener.on_upload_complete(
            None, self._msgs_dict, self._total_files, self._corrupted
        )
        return

    @retry(
        wait=wait_exponential(multiplier=2, min=4, max=8),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    async def _upload_file(self, cap_mono, file, o_path, force_document=False):
        if (
            self._thumb is not None
            and not await aiopath.exists(self._thumb)
            and self._thumb != "none"
        ):
            self._thumb = None
        thumb = self._thumb
        self._is_corrupted = False
        try:
            imgbb_thumb = None
            tmdb_poster_url = None
            is_video, is_audio, is_image = await get_document_type(self._up_path)

            if db is not None and is_video:
                if self._listener.user_dict.get("IMGBB_UPLOAD") and self._listener.thumbnail_layout:
                    imgbb_thumb = await get_multiple_frames_thumbnail(
                        self._up_path,
                        self._listener.thumbnail_layout,
                        self._listener.screen_shots,
                    )
                else:
                    imgbb_thumb = await get_video_thumbnail(self._up_path, None)

            if Config.TMDB_API_KEY and is_video:
                title = remove_redandent(ospath.splitext(file)[0])
                parsed_data = PTN.parse(title)
                title = parsed_data.get("title", "").replace("_", " ").replace("-", " ").replace(":", " ")
                title = ' '.join(title.split())
                aka_pattern = r'\sA[.\s]?K[.\s]?A[.]?\s+'
                if re.search(aka_pattern, title, re.IGNORECASE):
                    title = re.split(aka_pattern, title, maxsplit=1, flags=re.IGNORECASE)[0].strip()
                year = parsed_data.get("year")
                season = parsed_data.get("season")

                if season:
                    tmdb_poster_url = await get_tv_poster(title, year)
                else:
                    tmdb_poster_url = await get_movie_poster(title, year)

            if not is_image and thumb is None:
                file_name = ospath.splitext(file)[0]
                thumb_path = f"{self._path}/yt-dlp-thumb/{file_name}.jpg"
                if await aiopath.isfile(thumb_path):
                    thumb = thumb_path
                elif await aiopath.isfile(thumb_path.replace("/yt-dlp-thumb", "")):
                    thumb = thumb_path.replace("/yt-dlp-thumb", "")
                elif is_audio and not is_video:
                    thumb = await get_audio_thumbnail(self._up_path)

            if (
                self._listener.as_doc
                or force_document
                or (not is_video and not is_audio and not is_image)
            ):
                key = "documents"
                if tmdb_poster_url and thumb is None:
                    thumb =  await self.get_custom_thumb(tmdb_poster_url)
                    LOGGER.info("Got the poster")

                if is_video and thumb is None:
                    thumb = await get_video_thumbnail(self._up_path, None)

                if self._listener.is_cancelled:
                    return
                if thumb == "none":
                    thumb = None
                self._sent_msg = await self._sent_msg.reply_document(
                    document=self._up_path,
                    quote=True,
                    thumb=thumb,
                    caption=cap_mono,
                    force_document=True,
                    disable_notification=True,
                    progress=self._upload_progress,
                )
            elif is_video:
                key = "videos"
                duration = (await get_media_info(self._up_path))[0]
                if tmdb_poster_url and thumb is None:
                    thumb =  await self.get_custom_thumb(tmdb_poster_url)
                    LOGGER.info("Got the poster")
    
                if thumb is None and self._listener.thumbnail_layout:
                    thumb = await get_multiple_frames_thumbnail(
                        self._up_path,
                        self._listener.thumbnail_layout,
                        self._listener.screen_shots,
                    )
                if thumb is None:
                    thumb = await get_video_thumbnail(self._up_path, duration)
                if thumb is not None and thumb != "none":
                    with Image.open(thumb) as img:
                        width, height = img.size
                else:
                    width = 480
                    height = 320
                if self._listener.is_cancelled:
                    return
                if thumb == "none":
                    thumb = None
                self._sent_msg = await self._sent_msg.reply_video(
                    video=self._up_path,
                    quote=True,
                    caption=cap_mono,
                    duration=duration,
                    width=width,
                    height=height,
                    thumb=thumb,
                    supports_streaming=True,
                    disable_notification=True,
                    progress=self._upload_progress,
                )
            elif is_audio:
                key = "audios"
                duration, artist, title = await get_media_info(self._up_path)
                if self._listener.is_cancelled:
                    return
                if thumb == "none":
                    thumb = None
                self._sent_msg = await self._sent_msg.reply_audio(
                    audio=self._up_path,
                    quote=True,
                    caption=cap_mono,
                    duration=duration,
                    performer=artist,
                    title=title,
                    thumb=thumb,
                    disable_notification=True,
                    progress=self._upload_progress,
                )
            else:
                key = "photos"
                if self._listener.is_cancelled:
                    return
                self._sent_msg = await self._sent_msg.reply_photo(
                    photo=self._up_path,
                    quote=True,
                    caption=cap_mono,
                    disable_notification=True,
                    progress=self._upload_progress,
                )

            cpy_msg = await self._copy_message()

            if imgbb_thumb:
                await self._upload_to_imgbb(imgbb_thumb, file, cpy_msg)

            if (
                not self._listener.is_cancelled
                and self._media_group
                and (self._sent_msg.video or self._sent_msg.document)
            ):
                key = "documents" if self._sent_msg.document else "videos"
                if match := re_match(r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)", o_path):
                    pname = match.group(0)
                    if pname in self._media_dict[key].keys():
                        self._media_dict[key][pname].append(
                            [self._sent_msg.chat.id, self._sent_msg.id]
                        )
                    else:
                        self._media_dict[key][pname] = [
                            [self._sent_msg.chat.id, self._sent_msg.id]
                        ]
                    msgs = self._media_dict[key][pname]
                    if len(msgs) == 10:
                        await self._send_media_group(pname, key, msgs)
                    else:
                        self._last_msg_in_group = True

            if (
                self._thumb is None
                and thumb is not None
                and await aiopath.exists(thumb)
            ):
                await remove(thumb)
        except (FloodWait, FloodPremiumWait) as f:
            LOGGER.warning(str(f))
            await sleep(f.value * 1.3)
            if (
                self._thumb is None
                and thumb is not None
                and await aiopath.exists(thumb)
            ):
                await remove(thumb)
            return await self._upload_file(cap_mono, file, o_path)
        except Exception as err:
            if (
                self._thumb is None
                and thumb is not None
                and await aiopath.exists(thumb)
            ):
                await remove(thumb)
            err_type = "RPCError: " if isinstance(err, RPCError) else ""
            LOGGER.error(f"{err_type}{err}. Path: {self._up_path}")
            if isinstance(err, BadRequest) and key != "documents":
                LOGGER.error(f"Retrying As Document. Path: {self._up_path}")
                return await self._upload_file(cap_mono, file, o_path, True)
            raise err
        finally:
            if imgbb_thumb and await aiopath.exists(imgbb_thumb):
                await remove(imgbb_thumb)


    async def _upload_to_imgbb(self, imgbb_thumb, file, cpy_msg):
        try:
            if cpy_msg:
                f_name = await remove_extension(ospath.splitext(file)[0])
                imgbb_client = imgbbpy.AsyncClient(Config.IMGBB_API_KEY)
                if isinstance(cpy_msg, dict):
                    message_id = cpy_msg.get("message_id")
                else:
                    message_id = cpy_msg.id
                screenshot = await imgbb_client.upload(
                    file=imgbb_thumb, name=f"{message_id}"
                )
                ss_url = screenshot.url
                ss_del_url = screenshot.delete_url
                await imgbb_client.close()
                if isinstance(cpy_msg, dict):
                    file_info = cpy_msg
                else:
                    file_info = await extract_file_info(
                        cpy_msg, channel_id=cpy_msg.chat.id
                    )
                file_info["poster_url"] = ss_url
                file_info["poster_delete_url"] = ss_del_url
                await files_col.update_one(
                    {
                        "channel_id": file_info["channel_id"],
                        "message_id": file_info["message_id"],
                    },
                    {"$set": file_info},
                    upsert=True,
                )
                LOGGER.info(f"Uploaded screenshot to imgbb: {f_name}")
        except Exception as e:
            LOGGER.error(f"Error uploading to imgbb or MongoDB: {e}")
            await self.cancel_task()
            await self._sent_msg.reply_text(f"Error uploading to imgbb or MongoDB: {e}")

    async def _copy_message(self):
        await sleep(1)

        async def _copy(target, retries=3):
            cpy_msg = None
            for attempt in range(retries):
                try:
                    msg = await TgClient.bot.get_messages(
                        self._sent_msg.chat.id,
                        self._sent_msg.id,
                    )
                    if msg and (msg.document.mime_type.startswith("video/") 
                                or msg.document.file_name.lower().endswith(".srt")):
                        cpy_msg = await msg.copy(target)
                    return cpy_msg
                except Exception as e:
                    LOGGER.error(f"Attempt {attempt + 1} failed: {e} {msg.id}")
                    if attempt < retries - 1:
                        await sleep(0.5)
            LOGGER.error(f"Failed to copy message after {retries} attempts")
            return cpy_msg
        
        if self._user_dump:
            with contextlib.suppress(Exception):
                cpy_msg = await _copy(int(self._user_dump))
                return cpy_msg

    @property
    def speed(self):
        try:
            return self._processed_bytes / (time() - self._start_time)
        except:
            return 0

    @property
    def processed_bytes(self):
        return self._processed_bytes

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Upload: {self._listener.name}")
        await self._listener.on_upload_error("your upload has been stopped!")
