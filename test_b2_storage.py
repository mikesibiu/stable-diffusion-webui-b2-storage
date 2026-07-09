#!/usr/bin/env python3
"""
Unit tests for Stable Diffusion WebUI B2 Storage Extension.
Mocks the WebUI environment (modules.shared, modules.script_callbacks, gradio)
to verify settings registration and image saved callbacks.
"""

import sys
import os
from unittest.mock import MagicMock, patch

# Mock Stable Diffusion WebUI dependencies
mock_shared = MagicMock()
mock_script_callbacks = MagicMock()
mock_gradio = MagicMock()

# Setup default options dict
mock_shared.opts = MagicMock()
mock_shared.opts.data = {
    "b2_storage_enable": True,
    "b2_storage_api_type": "native",
    "b2_storage_key_id": "test_id",
    "b2_storage_application_key": "test_key",
    "b2_storage_bucket": "test_bucket",
    "b2_storage_delete_local": True
}

mock_modules = MagicMock()
mock_modules.shared = mock_shared
mock_modules.script_callbacks = mock_script_callbacks
sys.modules["modules"] = mock_modules
sys.modules["modules.shared"] = mock_shared
sys.modules["modules.script_callbacks"] = mock_script_callbacks
sys.modules["gradio"] = mock_gradio
sys.modules["requests"] = MagicMock()

# Ensure local directories are in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

import unittest

# Import the extension scripts
import scripts.b2_storage as b2_storage


class TestB2StorageExtension(unittest.TestCase):
    """Test suite for the WebUI extension callbacks."""

    def setUp(self):
        # Reset mock callbacks
        mock_shared.opts.add_option.reset_mock()
        
    def test_settings_registration(self):
        """Verify that options are registered on the Settings tab."""
        b2_storage.on_ui_settings()
        self.assertTrue(mock_shared.opts.add_option.called)
        # Check that essential keys were added
        called_args = [call[0][0] for call in mock_shared.opts.add_option.call_args_list]
        self.assertIn("b2_storage_enable", called_args)
        self.assertIn("b2_storage_api_type", called_args)
        self.assertIn("b2_storage_key_id", called_args)
        self.assertIn("b2_storage_application_key", called_args)
        self.assertIn("b2_storage_bucket", called_args)

    @patch("scripts.b2_storage.B2NativeAdapter")
    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    def test_image_saved_native_callback(self, mock_remove, mock_exists, mock_adapter_class):
        """Verify B2 Native upload flow and local file cleanup on image save."""
        # Setup B2 adapter mock instance
        mock_adapter = MagicMock()
        mock_adapter_class.return_value = mock_adapter

        # Configure settings to use native mode
        mock_shared.opts.data["b2_storage_api_type"] = "native"
        mock_shared.opts.data["b2_storage_delete_local"] = True

        # Mock callback parameter
        params = MagicMock()
        params.filename = "/Users/mfarace/ClaudeProjects/stable-diffusion/outputs/txt2img-images/00001.png"

        # Execute callback
        b2_storage.on_image_saved(params)

        # Verify adapter was initialized and called
        mock_adapter_class.assert_called_once_with("test_id", "test_key")
        mock_adapter.authenticate.assert_called_once()
        mock_adapter.upload_file.assert_called_once_with(
            os.path.abspath(params.filename),
            "outputs/txt2img-images/00001.png",
            "test_bucket"
        )
        
        # Verify local file was removed
        mock_remove.assert_called_once_with(os.path.abspath(params.filename))

    @patch("scripts.b2_storage.B2S3Adapter")
    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    def test_image_saved_s3_callback(self, mock_remove, mock_exists, mock_adapter_class):
        """Verify B2 S3 upload flow on image save."""
        # Setup B2 adapter mock instance
        mock_adapter = MagicMock()
        mock_adapter_class.return_value = mock_adapter

        # Configure settings to use S3 mode
        mock_shared.opts.data["b2_storage_api_type"] = "s3"
        mock_shared.opts.data["b2_storage_s3_endpoint"] = "https://s3.us-west-004.backblazeb2.com"
        mock_shared.opts.data["b2_storage_delete_local"] = False

        # Mock callback parameter
        params = MagicMock()
        params.filename = "/Users/mfarace/ClaudeProjects/stable-diffusion/outputs/txt2img-images/00001.png"

        # Execute callback
        b2_storage.on_image_saved(params)

        # Verify S3 adapter was initialized and called
        mock_adapter_class.assert_called_once_with(
            "test_id", "test_key", "https://s3.us-west-004.backblazeb2.com"
        )
        mock_adapter.authenticate.assert_called_once()
        
        # Verify local file was NOT removed
        mock_remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
