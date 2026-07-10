#!/usr/bin/env python3
"""
Unit tests for Stable Diffusion WebUI B2 Storage Extension.
Mocks the WebUI environment (modules.shared, modules.script_callbacks, gradio)
to verify settings registration, upload job queueing, adapter caching,
env-var credential fallback, and local file cleanup.
"""

import os
import sys
import queue
import unittest
from unittest.mock import MagicMock, patch

# Mock Stable Diffusion WebUI dependencies before importing the extension
mock_shared = MagicMock()
mock_script_callbacks = MagicMock()
mock_gradio = MagicMock()

mock_modules = MagicMock()
mock_modules.shared = mock_shared
mock_modules.script_callbacks = mock_script_callbacks
sys.modules["modules"] = mock_modules
sys.modules["modules.shared"] = mock_shared
sys.modules["modules.script_callbacks"] = mock_script_callbacks
sys.modules["gradio"] = mock_gradio
if "requests" not in sys.modules:
    try:
        import requests  # noqa: F401
    except ImportError:
        sys.modules["requests"] = MagicMock()

# Ensure local directories are in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

import scripts.b2_storage as b2_storage

# The extension resolves remote names relative to the WebUI root
# (two levels above the extension directory) — build test paths from it
# so tests pass on any machine.
WEBUI_ROOT = os.path.abspath(os.path.join(current_dir, "..", ".."))
TEST_IMAGE = os.path.join(WEBUI_ROOT, "outputs", "txt2img-images", "00001.png")

DEFAULT_SETTINGS = {
    "b2_storage_enable": True,
    "b2_storage_api_type": "native",
    "b2_storage_key_id": "test_id",
    "b2_storage_application_key": "test_key",
    "b2_storage_bucket": "test_bucket",
    "b2_storage_s3_endpoint": "",
    "b2_storage_delete_local": True,
}


class B2StorageTestCase(unittest.TestCase):
    """Shared fixture: fresh settings, empty queue/cache, no worker thread."""

    def setUp(self):
        mock_shared.opts.add_option.reset_mock()
        mock_shared.opts.data = dict(DEFAULT_SETTINGS)
        b2_storage._adapter_cache.clear()
        while not b2_storage._upload_queue.empty():
            b2_storage._upload_queue.get_nowait()
        # Keep the real worker thread out of unit tests
        worker_patch = patch.object(b2_storage, "_ensure_worker")
        worker_patch.start()
        self.addCleanup(worker_patch.stop)

    def enqueued_job(self):
        try:
            return b2_storage._upload_queue.get_nowait()
        except queue.Empty:
            return None

    def make_params(self, filename=TEST_IMAGE):
        params = MagicMock()
        params.filename = filename
        return params


class TestSettingsRegistration(B2StorageTestCase):

    def test_settings_registration(self):
        """Verify that options are registered on the Settings tab."""
        b2_storage.on_ui_settings()
        called_args = [call[0][0] for call in mock_shared.opts.add_option.call_args_list]
        for key in DEFAULT_SETTINGS:
            self.assertIn(key, called_args)


class TestImageSavedQueueing(B2StorageTestCase):

    @patch("os.path.exists", return_value=True)
    def test_image_saved_enqueues_upload_job(self, mock_exists):
        b2_storage.on_image_saved(self.make_params())

        job = self.enqueued_job()
        self.assertIsNotNone(job, "on_image_saved should enqueue an upload job")
        self.assertEqual(job["local_path"], os.path.abspath(TEST_IMAGE))
        self.assertEqual(job["remote_name"], "outputs/txt2img-images/00001.png")
        self.assertEqual(job["bucket"], "test_bucket")
        self.assertTrue(job["delete_local"])

    @patch("os.path.exists", return_value=True)
    def test_disabled_does_not_enqueue(self, mock_exists):
        mock_shared.opts.data["b2_storage_enable"] = False
        b2_storage.on_image_saved(self.make_params())
        self.assertIsNone(self.enqueued_job())

    @patch("os.path.exists", return_value=True)
    def test_missing_credentials_does_not_enqueue(self, mock_exists):
        mock_shared.opts.data["b2_storage_key_id"] = ""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("B2_APPLICATION_KEY_ID", None)
            b2_storage.on_image_saved(self.make_params())
        self.assertIsNone(self.enqueued_job())

    @patch("os.path.exists", return_value=True)
    def test_env_var_credential_fallback(self, mock_exists):
        """Empty settings fall back to standard B2 environment variables."""
        mock_shared.opts.data["b2_storage_key_id"] = ""
        mock_shared.opts.data["b2_storage_application_key"] = ""
        env = {"B2_APPLICATION_KEY_ID": "env_id", "B2_APPLICATION_KEY": "env_key"}
        with patch.dict(os.environ, env):
            b2_storage.on_image_saved(self.make_params())

        job = self.enqueued_job()
        self.assertIsNotNone(job)
        self.assertEqual(job["key_id"], "env_id")
        self.assertEqual(job["application_key"], "env_key")

    @patch("os.path.exists", return_value=True)
    def test_file_outside_webui_root_uses_basename(self, mock_exists):
        b2_storage.on_image_saved(self.make_params("/somewhere/else/00002.png"))
        job = self.enqueued_job()
        self.assertEqual(job["remote_name"], "00002.png")

    @patch("os.path.exists", return_value=True)
    def test_s3_type_without_endpoint_does_not_enqueue(self, mock_exists):
        mock_shared.opts.data["b2_storage_api_type"] = "s3"
        mock_shared.opts.data["b2_storage_s3_endpoint"] = ""
        b2_storage.on_image_saved(self.make_params())
        self.assertIsNone(self.enqueued_job())


    @patch("os.path.exists", return_value=True)
    def test_webui_at_filesystem_root_preserves_structure(self, mock_exists):
        """Extension two levels below / (e.g. /workspace/<repo>) must still
        compute relative remote names, not fall back to basename."""
        with patch.object(b2_storage, "extension_dir", "/workspace/b2-ext"):
            b2_storage.on_image_saved(self.make_params("/outputs/txt2img-images/00001.png"))
        job = self.enqueued_job()
        self.assertEqual(job["remote_name"], "outputs/txt2img-images/00001.png")

    @patch("os.path.exists", return_value=True)
    def test_sibling_dir_with_common_prefix_uses_basename(self, mock_exists):
        """A path in a sibling dir sharing a name prefix with the WebUI root
        (e.g. /sd-webui-backup vs /sd-webui) must not leak a mangled relative path."""
        with patch.object(b2_storage, "extension_dir", "/sd-webui/extensions/b2-ext"):
            b2_storage.on_image_saved(self.make_params("/sd-webui-backup/00002.png"))
        job = self.enqueued_job()
        self.assertEqual(job["remote_name"], "00002.png")


class TestUploadJobProcessing(B2StorageTestCase):

    @patch("scripts.b2_storage.B2NativeAdapter")
    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    def test_native_upload_and_local_cleanup(self, mock_remove, mock_exists, mock_adapter_class):
        """Verify B2 Native upload flow and local file cleanup."""
        mock_adapter = MagicMock()
        mock_adapter_class.return_value = mock_adapter

        b2_storage.on_image_saved(self.make_params())
        b2_storage._process_job(self.enqueued_job())

        mock_adapter_class.assert_called_once_with("test_id", "test_key")
        mock_adapter.authenticate.assert_called_once()
        mock_adapter.upload_file.assert_called_once_with(
            os.path.abspath(TEST_IMAGE),
            "outputs/txt2img-images/00001.png",
            "test_bucket"
        )
        mock_remove.assert_called_once_with(os.path.abspath(TEST_IMAGE))

    @patch("scripts.b2_storage.B2S3Adapter")
    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    def test_s3_upload_without_cleanup(self, mock_remove, mock_exists, mock_adapter_class):
        """Verify B2 S3 upload flow; local file kept when delete_local is off."""
        mock_adapter = MagicMock()
        mock_adapter_class.return_value = mock_adapter

        mock_shared.opts.data["b2_storage_api_type"] = "s3"
        mock_shared.opts.data["b2_storage_s3_endpoint"] = "https://s3.us-west-004.backblazeb2.com"
        mock_shared.opts.data["b2_storage_delete_local"] = False

        b2_storage.on_image_saved(self.make_params())
        b2_storage._process_job(self.enqueued_job())

        mock_adapter_class.assert_called_once_with(
            "test_id", "test_key", "https://s3.us-west-004.backblazeb2.com"
        )
        mock_adapter.authenticate.assert_called_once()
        mock_remove.assert_not_called()

    @patch("scripts.b2_storage.B2NativeAdapter")
    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    def test_adapter_reused_across_jobs(self, mock_remove, mock_exists, mock_adapter_class):
        """The adapter (and its auth session) is cached between uploads."""
        mock_adapter_class.return_value = MagicMock()

        for _ in range(2):
            b2_storage.on_image_saved(self.make_params())
            b2_storage._process_job(self.enqueued_job())

        mock_adapter_class.assert_called_once()
        mock_adapter_class.return_value.authenticate.assert_called_once()
        self.assertEqual(mock_adapter_class.return_value.upload_file.call_count, 2)

    @patch("scripts.b2_storage.B2NativeAdapter")
    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    def test_failed_upload_keeps_local_file_and_drops_cached_adapter(
            self, mock_remove, mock_exists, mock_adapter_class):
        from b2_storage_adapter import B2AdapterException
        mock_adapter = MagicMock()
        mock_adapter.upload_file.side_effect = B2AdapterException("boom")
        mock_adapter_class.return_value = mock_adapter

        b2_storage.on_image_saved(self.make_params())
        b2_storage._process_job(self.enqueued_job())  # must not raise

        mock_remove.assert_not_called()
        self.assertEqual(len(b2_storage._adapter_cache), 0)


if __name__ == "__main__":
    unittest.main()


class TestLogging(unittest.TestCase):

    def test_logger_does_not_propagate_to_root(self):
        """Prevents every message appearing twice in the WebUI console."""
        self.assertFalse(b2_storage.logger.propagate)
