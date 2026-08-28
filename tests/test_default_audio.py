import unittest
from unittest.mock import MagicMock, AsyncMock, patch
from json import dumps as json_dumps

from bot.helper.ext_utils.bot_utils import arg_parser
from bot.helper.ext_utils.media_utils import FFMpeg
from bot.helper.common import TaskConfig


class TestDefaultAudio(unittest.TestCase):

    def test_arg_parser_da(self):
        args = {
            "-da": "",
            "-n": "",
            "link": "",
        }
        input_list = ["https://example.com/video.mp4", "-da", "1", "-n", "test.mp4"]
        arg_parser(input_list, args)
        self.assertEqual(args["-da"], "1")
        self.assertEqual(args["-n"], "test.mp4")
        self.assertEqual(args["link"], "https://example.com/video.mp4")

    def test_arg_parser_da_0(self):
        args = {
            "-da": "",
            "link": "",
        }
        input_list = ["https://example.com/video.mp4", "-da", "0"]
        arg_parser(input_list, args)
        self.assertEqual(args["-da"], "0")

    def test_arg_parser_da_bool(self):
        args = {
            "-da": "",
            "-n": "",
        }
        input_list = ["-da", "-n", "name"]
        arg_parser(input_list, args)
        self.assertTrue(args["-da"])
        self.assertEqual(args["-n"], "name")


class TestFFMpegDefaultAudioAsync(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.listener = MagicMock()
        self.listener.is_cancelled = False
        self.ffmpeg = FFMpeg(self.listener)

    @patch("bot.helper.ext_utils.media_utils.cmd_exec")
    @patch("bot.helper.ext_utils.media_utils.get_media_info", new_callable=AsyncMock)
    async def test_change_default_audio_out_of_bounds(self, mock_get_info, mock_cmd_exec):
        mock_get_info.return_value = (100, "Artist", "Title")
        # Video with only 1 audio stream (index 0)
        ffprobe_json = json_dumps({
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        })
        mock_cmd_exec.return_value = (ffprobe_json, "", 0)

        # Request audio_index = 2 (out of bounds)
        res = await self.ffmpeg.change_default_audio("test.mkv", 2)
        self.assertEqual(res, "test.mkv")

    @patch("bot.helper.ext_utils.media_utils.cmd_exec")
    @patch("bot.helper.ext_utils.media_utils.get_media_info", new_callable=AsyncMock)
    async def test_change_default_audio_no_audio(self, mock_get_info, mock_cmd_exec):
        mock_get_info.return_value = (100, "Artist", "Title")
        ffprobe_json = json_dumps({
            "streams": [
                {"codec_type": "video", "codec_name": "h264"}
            ]
        })
        mock_cmd_exec.return_value = (ffprobe_json, "", 0)

        res = await self.ffmpeg.change_default_audio("test.mkv", 0)
        self.assertEqual(res, "test.mkv")

    @patch("bot.helper.ext_utils.media_utils.cmd_exec")
    @patch("bot.helper.ext_utils.media_utils.get_media_info", new_callable=AsyncMock)
    @patch("bot.helper.ext_utils.media_utils.create_subprocess_exec")
    @patch("bot.helper.ext_utils.media_utils.remove", new_callable=AsyncMock)
    @patch("bot.helper.ext_utils.media_utils.move", new_callable=AsyncMock)
    async def test_change_default_audio_success(
        self, mock_move, mock_remove, mock_subproc, mock_get_info, mock_cmd_exec
    ):
        mock_get_info.return_value = (100, "Artist", "Title")
        ffprobe_json = json_dumps({
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
                {"codec_type": "audio", "codec_name": "ac3"},
            ]
        })
        mock_cmd_exec.return_value = (ffprobe_json, "", 0)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.stdout.at_eof.return_value = True
        mock_proc.communicate.return_value = (b"", b"")
        mock_subproc.return_value = mock_proc

        res = await self.ffmpeg.change_default_audio("test.mkv", 1)
        self.assertEqual(res, "test.mkv")
        mock_subproc.assert_called_once()
        cmd_args = mock_subproc.call_args[0]
        self.assertIn("-disposition:a", cmd_args)
        self.assertIn("-disposition:a:1", cmd_args)
        self.assertIn("default", cmd_args)

    @patch("bot.helper.common.get_document_type", new_callable=AsyncMock)
    @patch("bot.helper.common.FFMpeg")
    @patch("bot.helper.common.task_dict_lock", AsyncMock())
    @patch("bot.helper.common.cpu_eater_lock")
    async def test_proceed_default_audio(self, mock_cpu_lock, mock_ffmpeg_cls, mock_get_doc_type):
        mock_cpu_lock.acquire = AsyncMock()
        mock_cpu_lock.release = MagicMock()

        mock_get_doc_type.return_value = (True, False, False)
        mock_ffmpeg_inst = AsyncMock()
        mock_ffmpeg_cls.return_value = mock_ffmpeg_inst
        mock_ffmpeg_inst.change_default_audio.return_value = "video.mp4"

        task_config = TaskConfig.__new__(TaskConfig)
        task_config.mid = 123
        task_config.is_file = True
        task_config.is_cancelled = False
        task_config.default_audio = "1"
        task_config.size = 1000
        task_config.subsize = 0
        task_config.subname = ""
        task_config.proceed_count = 0
        task_config.progress = True

        res = await task_config.proceed_default_audio("video.mp4", "gid123")
        self.assertEqual(res, "video.mp4")
        mock_ffmpeg_inst.change_default_audio.assert_called_once_with("video.mp4", "1")


if __name__ == "__main__":
    unittest.main()
