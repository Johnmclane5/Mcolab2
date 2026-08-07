import os
import time
import httpx
import aiofiles
from ... import LOGGER, task_dict_lock, task_dict
from ...core.config_manager import Config
from ..telegram_helper.message_utils import send_message
from ..ext_utils.files_utils import count_files_and_folders


class ProgressSender:
    def __init__(self, file_path, uploader):
        self.file_path = file_path
        self.uploader = uploader
        self.file_size = os.path.getsize(file_path)
        self._bytes_read = 0

    async def _read_file(self):
        async with aiofiles.open(self.file_path, "rb") as f:
            while True:
                if self.uploader.listener.is_cancelled:
                    raise Exception("Upload cancelled by user!")
                chunk = await f.read(65536)
                if not chunk:
                    break
                self._bytes_read += len(chunk)
                self.uploader.processed_bytes += len(chunk)
                yield chunk

    def __aiter__(self):
        return self._read_file()


class BuzzheavierUploader:
    def __init__(self, listener, path):
        self.listener = listener
        self.path = path
        self.processed_bytes = 0
        self.total_size = listener.size
        self.token = Config.BUZZHEAVIER_TOKEN or ""
        self._start_time = time.time()
        self.uploaded_files = {}  # maps filename -> link

    @property
    def speed(self):
        elapsed = time.time() - self._start_time
        if elapsed > 0:
            return self.processed_bytes / elapsed
        return 0

    async def _get_root_id(self):
        if not self.token:
            return None
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient() as client:
            # 1. Try GET /api/account
            try:
                res = await client.get("https://buzzheavier.com/api/account", headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    for key in ["rootDirectoryId", "rootFolderId", "root_id", "root", "id"]:
                        if key in data:
                            return data[key]
                        if isinstance(data.get("data"), dict):
                            for subkey in ["rootDirectoryId", "rootFolderId", "root_id", "root", "id"]:
                                if subkey in data["data"]:
                                    return data["data"][subkey]
            except Exception as e:
                LOGGER.error(f"Error fetching account info: {e}")

            # 2. Try GET /api/fs
            try:
                res = await client.get("https://buzzheavier.com/api/fs", headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, dict):
                        for key in ["id", "root", "parentId", "parent_id"]:
                            if key in data:
                                return data[key]
                        if isinstance(data.get("data"), dict):
                            for subkey in ["id", "root", "parentId", "parent_id"]:
                                if subkey in data["data"]:
                                    return data["data"][subkey]
            except Exception as e:
                LOGGER.error(f"Error fetching fs info: {e}")
        return None

    async def create_folder(self, name, parent_id=None):
        if not self.token:
            return None
        if parent_id is None:
            parent_id = await self._get_root_id()

        url = f"https://buzzheavier.com/api/fs/{parent_id}" if parent_id else "https://buzzheavier.com/api/fs"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {"name": name}
        async with httpx.AsyncClient() as client:
            try:
                res = await client.post(url, headers=headers, json=payload)
                if res.status_code in [200, 201]:
                    data = res.json()
                    folder_id = None
                    if isinstance(data, dict):
                        if "data" in data and isinstance(data["data"], dict):
                            folder_id = data["data"].get("id")
                        if not folder_id:
                            folder_id = data.get("id")
                    return folder_id
                else:
                    LOGGER.error(f"Failed to create folder {name}: status={res.status_code}, response={res.text}")
            except Exception as e:
                LOGGER.error(f"Error creating folder {name}: {e}")
        return None

    async def _upload_file(self, file_path, parent_id=None):
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        if self.token and parent_id:
            url = f"https://w.buzzheavier.com/{parent_id}/{filename}"
        else:
            url = f"https://w.buzzheavier.com/{filename}"

        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers["Content-Length"] = str(file_size)

        progress_sender = ProgressSender(file_path, self)

        async with httpx.AsyncClient(timeout=None) as client:
            try:
                res = await client.put(url, headers=headers, content=progress_sender)
                if res.status_code in [200, 201]:
                    data = res.json()
                    file_id = None
                    if isinstance(data, dict):
                        if "data" in data and isinstance(data["data"], dict):
                            file_id = data["data"].get("id")
                        if not file_id:
                            file_id = data.get("id")
                    if file_id:
                        return f"https://buzzheavier.com/{file_id}"
                    else:
                        LOGGER.error(f"Upload succeeded but couldn't parse file ID from response: {res.text}")
                else:
                    LOGGER.error(f"Upload failed for {filename}: status={res.status_code}, response={res.text}")
            except Exception as e:
                LOGGER.error(f"Error uploading file {filename}: {e}")
        return None

    async def _upload_directory(self, dir_path, parent_id=None):
        dir_name = os.path.basename(dir_path)

        if self.token:
            current_folder_id = await self.create_folder(dir_name, parent_id)
            if not current_folder_id:
                current_folder_id = parent_id
        else:
            current_folder_id = None

        uploaded_items = {}
        for item in sorted(os.listdir(dir_path)):
            if self.listener.is_cancelled:
                raise Exception("Upload cancelled by user!")
            item_path = os.path.join(dir_path, item)
            if os.path.isdir(item_path):
                sub_items = await self._upload_directory(item_path, current_folder_id)
                uploaded_items.update(sub_items)
            else:
                file_url = await self._upload_file(item_path, current_folder_id)
                if file_url:
                    uploaded_items[item] = file_url
                    self.uploaded_files[item] = file_url

        return uploaded_items

    async def upload(self):
        try:
            self._start_time = time.time()
            if os.path.isdir(self.path):
                mime_type = "Folder"
                folders, files = await count_files_and_folders(self.path)

                if self.token:
                    # Authenticated Folder Structure
                    root_parent = await self._get_root_id()
                    folder_name = os.path.basename(self.path)
                    top_folder_id = await self.create_folder(folder_name, root_parent)

                    if not top_folder_id:
                        await self.listener.on_upload_error("Failed to create root folder on Buzzheavier!")
                        return

                    # Upload all contents recursively into this top_folder_id
                    for item in sorted(os.listdir(self.path)):
                        if self.listener.is_cancelled:
                            raise Exception("Upload cancelled by user!")
                        item_path = os.path.join(self.path, item)
                        if os.path.isdir(item_path):
                            await self._upload_directory(item_path, top_folder_id)
                        else:
                            file_url = await self._upload_file(item_path, top_folder_id)
                            if file_url:
                                self.uploaded_files[item] = file_url

                    link = f"https://buzzheavier.com/{top_folder_id}"
                else:
                    # Anonymous Multi-file Upload (flat structure)
                    for root, dirs, filenames in os.walk(self.path):
                        for filename in sorted(filenames):
                            if self.listener.is_cancelled:
                                raise Exception("Upload cancelled by user!")
                            file_path = os.path.join(root, filename)
                            file_url = await self._upload_file(file_path)
                            if file_url:
                                self.uploaded_files[filename] = file_url

                    if not self.uploaded_files:
                        await self.listener.on_upload_error("No files uploaded successfully!")
                        return

                    link = list(self.uploaded_files.values())[0]

                    # For anonymous multiple-file upload, since there's no folder view,
                    # we send a message listing all generated download links.
                    if len(self.uploaded_files) > 1:
                        links_msg = "<b>Buzzheavier Uploaded Files:</b>\n"
                        for name, flink in self.uploaded_files.items():
                            links_msg += f"- <a href='{flink}'>{name}</a>\n"
                        await send_message(self.listener.message, links_msg)

            else:
                # Single File Upload
                mime_type = "file"
                folders = 0
                files = 1
                link = await self._upload_file(self.path)
                if not link:
                    await self.listener.on_upload_error("Failed to upload file to Buzzheavier!")
                    return
                self.uploaded_files[os.path.basename(self.path)] = link

            if self.listener.is_cancelled:
                return

            LOGGER.info(f"Buzzheavier Upload Completed. Link: {link}")
            await self.listener.on_upload_complete(
                link,
                files,
                folders,
                mime_type
            )
        except Exception as e:
            LOGGER.error(f"Buzzheavier Upload Error: {e}")
            await self.listener.on_upload_error(str(e))
