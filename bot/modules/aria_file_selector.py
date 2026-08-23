from asyncio import wait_for, Event, sleep
from functools import partial
from pyrogram.filters import regex, user
from pyrogram.handlers import CallbackQueryHandler
from aiofiles.os import remove, path as aiopath

from .. import LOGGER, task_dict_lock
from ..core.torrent_manager import TorrentManager
from ..helper.ext_utils.bot_utils import new_task, humanbytes
from ..helper.ext_utils.status_utils import get_task_by_gid
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    send_message,
    send_status_message,
    edit_message,
    delete_message,
)

FILES_PER_PAGE = 5


class AriaTorrentSelector:
    def __init__(self, listener, gid):
        self.listener = listener
        self.gid = gid
        self.event = Event()
        self.reply_to = None
        self.page = 0
        self.files = []  # list of dicts: {"index": str, "path": str, "name": str, "length": int, "selected": bool}
        self.timeout = 600

    async def _fetch_files(self):
        res = await TorrentManager.aria2.getFiles(self.gid)
        self.files = []
        for f in res:
            idx = str(f["index"])
            name = f["path"].split("/")[-1] or f["path"]
            length = int(f.get("length", 0))
            selected = f.get("selected", "true") == "true"
            self.files.append({
                "index": idx,
                "path": f["path"],
                "name": name,
                "length": length,
                "selected": selected,
            })

    def _build_buttons(self):
        buttons = ButtonMaker()
        total_files = len(self.files)
        total_pages = (total_files + FILES_PER_PAGE - 1) // FILES_PER_PAGE or 1
        if self.page >= total_pages:
            self.page = total_pages - 1
        if self.page < 0:
            self.page = 0

        start_idx = self.page * FILES_PER_PAGE
        end_idx = min(start_idx + FILES_PER_PAGE, total_files)
        page_files = self.files[start_idx:end_idx]

        for f in page_files:
            status = "✅" if f["selected"] else "❌"
            size_str = humanbytes(f["length"])
            btn_text = f"{status} {f['name']} ({size_str})"
            # data format: aria_sel toggle <gid> <index>
            buttons.data_button(btn_text, f"aria_sel toggle {self.gid} {f['index']}")

        # Action buttons row 1: Toggle Page / Select All / Deselect All / Invert
        all_sel = all(f["selected"] for f in self.files)
        if all_sel:
            buttons.data_button("Deselect All", f"aria_sel deselect_all {self.gid}")
        else:
            buttons.data_button("Select All", f"aria_sel select_all {self.gid}")

        buttons.data_button("Invert Page", f"aria_sel invert_page {self.gid}")

        # Navigation row
        if total_pages > 1:
            if self.page > 0:
                buttons.data_button("◀️ Prev", f"aria_sel prev {self.gid}")
            buttons.data_button(f"{self.page + 1}/{total_pages}", f"aria_sel page_info {self.gid}")
            if self.page < total_pages - 1:
                buttons.data_button("Next ▶️", f"aria_sel next {self.gid}")

        # Final row: Done / Cancel
        buttons.data_button("Done Selecting", f"aria_sel done {self.gid}")
        buttons.data_button("Cancel", f"aria_sel cancel {self.gid}")

        return buttons.build_menu(1)

    def _get_msg_text(self):
        sel_count = sum(1 for f in self.files if f["selected"])
        sel_size = sum(f["length"] for f in self.files if f["selected"])
        tot_size = sum(f["length"] for f in self.files)
        return (
            f"<b>Torrent File Selector for {self.listener.name or 'Torrent'}</b>\n\n"
            f"Selected: <b>{sel_count}/{len(self.files)}</b> | Size: <b>{humanbytes(sel_size)} / {humanbytes(tot_size)}</b>\n"
            f"Press file buttons to toggle selection, then press Done Selecting."
        )

    async def get_buttons(self):
        await self._fetch_files()
        return self._get_msg_text(), self._build_buttons()


@new_task
async def aria_selection_handler(_, query):
    user_id = query.from_user.id
    data = query.data.split()
    action = data[1]
    gid = data[2]

    task = await get_task_by_gid(gid)
    if task is None:
        await query.answer("This task has been cancelled!", show_alert=True)
        if query.message:
            await delete_message(query.message)
        return

    if user_id != task.listener.user_id:
        await query.answer("This task is not for you!", show_alert=True)
        return

    selector = getattr(task.listener, "aria_selector", None)
    if not selector:
        await query.answer("Selector expired or invalid!", show_alert=True)
        return

    if action == "page_info":
        await query.answer(f"Page {selector.page + 1}", show_alert=True)
        return

    if action == "toggle":
        idx = data[3]
        for f in selector.files:
            if f["index"] == idx:
                f["selected"] = not f["selected"]
                break
        await query.answer()
    elif action == "select_all":
        for f in selector.files:
            f["selected"] = True
        await query.answer("Selected all files!")
    elif action == "deselect_all":
        for f in selector.files:
            f["selected"] = False
        await query.answer("Deselected all files!")
    elif action == "invert_page":
        start_idx = selector.page * FILES_PER_PAGE
        end_idx = min(start_idx + FILES_PER_PAGE, len(selector.files))
        for f in selector.files[start_idx:end_idx]:
            f["selected"] = not f["selected"]
        await query.answer("Inverted page selection!")
    elif action == "prev":
        if selector.page > 0:
            selector.page -= 1
        await query.answer()
    elif action == "next":
        total_pages = (len(selector.files) + FILES_PER_PAGE - 1) // FILES_PER_PAGE
        if selector.page < total_pages - 1:
            selector.page += 1
        await query.answer()
    elif action == "done":
        selected_indexes = [f["index"] for f in selector.files if f["selected"]]
        if not selected_indexes:
            await query.answer("You must select at least one file!", show_alert=True)
            return

        await query.answer("Selection applied!")
        select_str = ",".join(selected_indexes)
        try:
            await TorrentManager.aria2.changeOption(gid, {"select-file": select_str})
        except Exception as e:
            LOGGER.error(f"Error changing select-file option in aria2: {e}")

        # Clean unselected files if any already created on disk
        for f in selector.files:
            if not f["selected"] and await aiopath.exists(f["path"]):
                try:
                    await remove(f["path"])
                except Exception as e:
                    LOGGER.error(f"Error removing unselected file {f['path']}: {e}")

        if not task.queued:
            try:
                await TorrentManager.aria2.unpause(gid)
            except Exception as e:
                LOGGER.error(f"Error unpausing torrent {gid}: {e}")

        await send_status_message(query.message)
        await delete_message(query.message)
        selector.event.set()
        return
    elif action == "cancel":
        await query.answer("Task cancelled!")
        await delete_message(query.message)
        selector.event.set()
        await task.cancel_task()
        return

    # Update inline keyboard and message text
    text = selector._get_msg_text()
    buttons = selector._build_buttons()
    await edit_message(query.message, text, buttons)
