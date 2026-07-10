import base64
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "01_transcribe_video.py"


def load_module():
    spec = importlib.util.spec_from_file_location("session_notes_transcribe", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPT_PATH.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class FakeResponse:
    status_code = 200
    text = '{"result":{"text":"hello","utterances":[]}}'
    headers = {
        "X-Api-Status-Code": "20000000",
        "X-Api-Message": "OK",
        "X-Tt-Logid": "test-log-id",
    }

    def json(self):
        return {"result": {"text": "hello", "utterances": []}}


class DirectUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_flash_request_embeds_local_audio_and_returns_result(self):
        audio_bytes = b"local-audio"
        with tempfile.NamedTemporaryFile(suffix=".mp3") as audio:
            audio.write(audio_bytes)
            audio.flush()
            transcriber = self.module.AudioTranscriber(api_key="test-api-key")

            with patch.object(self.module.requests, "post", return_value=FakeResponse()) as post:
                result = transcriber.recognize_audio(audio.name, lang="zh-CN")

        self.assertEqual(result["result"]["text"], "hello")
        _, kwargs = post.call_args
        self.assertEqual(
            kwargs["json"]["audio"]["data"],
            base64.b64encode(audio_bytes).decode("ascii"),
        )
        self.assertNotIn("url", kwargs["json"]["audio"])
        self.assertEqual(
            kwargs["headers"]["X-Api-Resource-Id"],
            "volc.bigasr.auc_turbo",
        )
        self.assertTrue(post.call_args.args[0].endswith("/api/v3/auc/bigmodel/recognize/flash"))

    def test_source_has_no_cloudflare_or_boto_dependency(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("R2Uploader", source)
        self.assertNotIn("boto3", source)
        self.assertNotIn("--r2-", source)

    def test_audio_over_two_hours_is_rejected_before_upload(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3") as audio:
            transcriber = self.module.AudioTranscriber(api_key="test-api-key")
            with (
                patch.object(self.module, "probe_media_duration_seconds", return_value=7201),
                patch.object(self.module.requests, "post") as post,
            ):
                with self.assertRaisesRegex(ValueError, "2 hours"):
                    transcriber.recognize_audio(audio.name)
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
