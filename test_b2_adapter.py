#!/usr/bin/env python3
"""
Unit tests for the Backblaze B2 storage adapters.
Mocks the HTTP layer (requests) to verify protocol behavior:
auth response parsing, restricted keys, token refresh, upload retry,
pagination, and URL encoding.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

# Provide a stub if 'requests' isn't installed in the test environment.
try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = MagicMock()

import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from b2_storage_adapter import B2NativeAdapter, B2AdapterException


def make_response(json_data=None, status_code=200, raise_error=None, headers=None):
    """Build a fake requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.headers = headers or {}
    if raise_error:
        resp.raise_for_status.side_effect = raise_error
    return resp


# b2_authorize_account v3 response shape (apiUrl/downloadUrl nested in apiInfo.storageApi)
V3_AUTH_RESPONSE = {
    "accountId": "acct123",
    "authorizationToken": "token_abc",
    "apiInfo": {
        "storageApi": {
            "apiUrl": "https://api004.backblazeb2.com",
            "downloadUrl": "https://f004.backblazeb2.com",
        }
    },
}

# Same, but for an application key restricted to a single bucket
V3_AUTH_RESTRICTED = {
    "accountId": "acct123",
    "authorizationToken": "token_abc",
    "apiInfo": {
        "storageApi": {
            "apiUrl": "https://api004.backblazeb2.com",
            "downloadUrl": "https://f004.backblazeb2.com",
            "bucketId": "bucket_id_1",
            "bucketName": "my-bucket",
        }
    },
}

# Legacy v2 response shape (top-level apiUrl/downloadUrl, 'allowed' object)
V2_AUTH_RESPONSE = {
    "accountId": "acct123",
    "authorizationToken": "token_abc",
    "apiUrl": "https://api004.backblazeb2.com",
    "downloadUrl": "https://f004.backblazeb2.com",
    "allowed": {"bucketId": "bucket_id_1", "bucketName": "my-bucket"},
}


class TestB2NativeAdapterAuth(unittest.TestCase):

    @patch("b2_storage_adapter.requests")
    def test_authenticate_parses_v3_response(self, mock_requests):
        mock_requests.get.return_value = make_response(V3_AUTH_RESPONSE)
        adapter = B2NativeAdapter("key_id", "app_key")
        adapter.authenticate()
        self.assertEqual(adapter.api_url, "https://api004.backblazeb2.com")
        self.assertEqual(adapter.download_url, "https://f004.backblazeb2.com")
        self.assertEqual(adapter.auth_token, "token_abc")

    @patch("b2_storage_adapter.requests")
    def test_authenticate_parses_v2_response(self, mock_requests):
        mock_requests.get.return_value = make_response(V2_AUTH_RESPONSE)
        adapter = B2NativeAdapter("key_id", "app_key")
        adapter.authenticate()
        self.assertEqual(adapter.api_url, "https://api004.backblazeb2.com")
        self.assertEqual(adapter.allowed_bucket_id, "bucket_id_1")
        self.assertEqual(adapter.allowed_bucket_name, "my-bucket")


class TestB2NativeAdapterRestrictedKeys(unittest.TestCase):
    """Keys restricted to one bucket cannot call b2_list_buckets account-wide."""

    @patch("b2_storage_adapter.requests")
    def test_restricted_key_uses_allowed_bucket_without_listing(self, mock_requests):
        mock_requests.get.return_value = make_response(V3_AUTH_RESTRICTED)
        adapter = B2NativeAdapter("key_id", "app_key")
        adapter.authenticate()

        bucket_id = adapter._get_bucket_id("my-bucket")

        self.assertEqual(bucket_id, "bucket_id_1")
        mock_requests.post.assert_not_called()

    @patch("b2_storage_adapter.requests")
    def test_restricted_key_rejects_other_bucket(self, mock_requests):
        mock_requests.get.return_value = make_response(V3_AUTH_RESTRICTED)
        adapter = B2NativeAdapter("key_id", "app_key")
        adapter.authenticate()

        with self.assertRaises(B2AdapterException):
            adapter._get_bucket_id("some-other-bucket")
        mock_requests.post.assert_not_called()


class TestB2NativeAdapterTokenRefresh(unittest.TestCase):

    @patch("b2_storage_adapter.requests")
    def test_reauthenticates_on_expired_token(self, mock_requests):
        """A 401 on an API call triggers one re-auth and a retry."""
        mock_requests.get.return_value = make_response(V3_AUTH_RESPONSE)
        expired = make_response({"code": "expired_auth_token"}, status_code=401)
        ok = make_response({"buckets": [{"bucketName": "my-bucket", "bucketId": "bid1"}]})
        mock_requests.post.side_effect = [expired, ok]

        adapter = B2NativeAdapter("key_id", "app_key")
        adapter.authenticate()
        bucket_id = adapter._get_bucket_id("my-bucket")

        self.assertEqual(bucket_id, "bid1")
        self.assertEqual(mock_requests.post.call_count, 2)
        self.assertEqual(mock_requests.get.call_count, 2)  # initial auth + re-auth


class TestB2NativeAdapterUpload(unittest.TestCase):

    def setUp(self):
        patcher = patch("b2_storage_adapter.requests")
        self.mock_requests = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_requests.get.return_value = make_response(V3_AUTH_RESTRICTED)

        # Keep retry backoff sleeps out of test runtime
        time_patcher = patch("b2_storage_adapter.time", create=True)
        self.mock_time = time_patcher.start()
        self.addCleanup(time_patcher.stop)

        self.adapter = B2NativeAdapter("key_id", "app_key")
        self.adapter.authenticate()

        # A real temp file to upload
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        self.tmp.write(b"fake image data")
        self.tmp.close()
        self.addCleanup(os.unlink, self.tmp.name)

    def upload_url_response(self):
        return make_response({
            "uploadUrl": "https://pod.backblazeb2.com/upload",
            "authorizationToken": "upload_token",
        })

    def test_upload_retries_with_new_upload_url(self):
        """B2 protocol: on a retryable upload failure, fetch a new upload URL and retry."""
        failed_upload = make_response({}, status_code=503)
        ok_upload = make_response({"fileId": "fid1"})
        self.mock_requests.post.side_effect = [
            self.upload_url_response(), failed_upload,   # attempt 1
            self.upload_url_response(), ok_upload,       # attempt 2
        ]

        url = self.adapter.upload_file(self.tmp.name, "outputs/img.png", "my-bucket")

        self.assertEqual(self.mock_requests.post.call_count, 4)
        self.assertIn("img.png", url)

    def test_upload_fails_after_max_attempts(self):
        failed = make_response({}, status_code=503)
        self.mock_requests.post.side_effect = [
            self.upload_url_response(), failed,
            self.upload_url_response(), failed,
            self.upload_url_response(), failed,
        ]
        with self.assertRaises(B2AdapterException):
            self.adapter.upload_file(self.tmp.name, "outputs/img.png", "my-bucket")

    def test_upload_reuses_upload_url_between_files(self):
        """Checklist: multiple files may share one upload URL/token until an error occurs."""
        self.mock_requests.post.side_effect = [
            self.upload_url_response(),
            make_response({"fileId": "fid1"}),
            make_response({"fileId": "fid2"}),  # second upload, no new URL fetch
        ]

        self.adapter.upload_file(self.tmp.name, "outputs/img1.png", "my-bucket")
        self.adapter.upload_file(self.tmp.name, "outputs/img2.png", "my-bucket")

        self.assertEqual(self.mock_requests.post.call_count, 3)
        get_url_calls = [
            c for c in self.mock_requests.post.call_args_list
            if "b2_get_upload_url" in str(c.args[0] if c.args else c.kwargs.get("url", ""))
        ]
        self.assertEqual(len(get_url_calls), 1)

    def test_upload_does_not_retry_forbidden(self):
        """Checklist: 403 means account cap/alert — do not retry, tell the user."""
        self.mock_requests.post.side_effect = [
            self.upload_url_response(),
            make_response({}, status_code=403),
        ]

        with self.assertRaises(B2AdapterException) as ctx:
            self.adapter.upload_file(self.tmp.name, "outputs/img.png", "my-bucket")

        self.assertEqual(self.mock_requests.post.call_count, 2)  # no retry
        self.assertIn("cap", str(ctx.exception).lower())

    def test_upload_retry_honors_retry_after_header(self):
        """Checklist: respect Retry-After on 503, else exponential backoff from 1s."""
        throttled = make_response({}, status_code=503, headers={"Retry-After": "3"})
        ok_upload = make_response({"fileId": "fid1"})
        self.mock_requests.post.side_effect = [
            self.upload_url_response(), throttled,
            self.upload_url_response(), ok_upload,
        ]

        self.adapter.upload_file(self.tmp.name, "outputs/img.png", "my-bucket")

        self.mock_time.sleep.assert_called_once_with(3.0)

    def test_upload_sends_user_agent_and_source_mtime(self):
        """Checklist: identify the client via User-Agent and set src_last_modified_millis."""
        self.mock_requests.post.side_effect = [
            self.upload_url_response(),
            make_response({"fileId": "fid1"}),
        ]

        self.adapter.upload_file(self.tmp.name, "outputs/img.png", "my-bucket")

        upload_headers = self.mock_requests.post.call_args_list[-1].kwargs["headers"]
        self.assertTrue(upload_headers["User-Agent"].startswith("sd-webui-b2-storage/"))
        expected_mtime = str(int(os.path.getmtime(self.tmp.name) * 1000))
        self.assertEqual(upload_headers["X-Bz-Info-src_last_modified_millis"], expected_mtime)

        # Auth call must identify the client too
        auth_headers = self.mock_requests.get.call_args.kwargs["headers"]
        self.assertIn("sd-webui-b2-storage/", auth_headers["User-Agent"])

    def test_upload_returns_percent_encoded_url(self):
        ok_upload = make_response({"fileId": "fid1"})
        self.mock_requests.post.side_effect = [self.upload_url_response(), ok_upload]

        url = self.adapter.upload_file(self.tmp.name, "outputs/my image (1).png", "my-bucket")

        self.assertEqual(
            url,
            "https://f004.backblazeb2.com/file/my-bucket/outputs/my%20image%20%281%29.png",
        )


class TestB2NativeAdapterListFiles(unittest.TestCase):

    @patch("b2_storage_adapter.requests")
    def test_list_files_paginates(self, mock_requests):
        """Follows nextFileName until the listing is exhausted."""
        mock_requests.get.return_value = make_response(V3_AUTH_RESTRICTED)
        page1 = make_response({
            "files": [{"fileName": "a.png", "contentLength": 1, "fileId": "f1", "uploadTimestamp": 100}],
            "nextFileName": "b.png",
        })
        page2 = make_response({
            "files": [{"fileName": "b.png", "contentLength": 2, "fileId": "f2", "uploadTimestamp": 200}],
            "nextFileName": None,
        })
        mock_requests.post.side_effect = [page1, page2]

        adapter = B2NativeAdapter("key_id", "app_key")
        adapter.authenticate()
        files = adapter.list_files("my-bucket")

        self.assertEqual([f["name"] for f in files], ["a.png", "b.png"])
        self.assertEqual(mock_requests.post.call_count, 2)
        # Second request must resume from nextFileName
        second_payload = mock_requests.post.call_args_list[1].kwargs["json"]
        self.assertEqual(second_payload["startFileName"], "b.png")


if __name__ == "__main__":
    unittest.main()
