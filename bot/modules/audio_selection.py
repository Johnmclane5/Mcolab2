from .. import (
    task_dict,
    task_dict_lock,
    user_data,
    LOGGER,
)
from ..helper.ext_utils.bot_utils import new_task

@new_task
async def audio_selection_callback(_, query):
    user_id = query.from_user.id
    data = query.data.split()
    mid = int(data[1])

    async with task_dict_lock:
        task = task_dict.get(mid)

    if task is None:
        await query.answer("This task has been cancelled!", show_alert=True)
        return

    if (
        user_id != task.user_id
        and (user_id not in user_data or not user_data[user_id].get("SUDO"))
    ):
        await query.answer("This task is not for you!", show_alert=True)
        return

    await query.answer()

    action = data[2]
    task.audio_data = action
    task.audio_event.set()
