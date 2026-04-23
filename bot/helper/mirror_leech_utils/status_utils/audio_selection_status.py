from .ffmpeg_status import FFmpegStatus
from .telegram_status import TelegramStatus

class AudioSelectionStatus(TelegramStatus):
    def __init__(self, listener, gid):
        super().__init__(listener, None, gid, "as")

    def status(self):
        return "Waiting for Audio Selection"
