#!/usr/bin/env python3
"""
Live integration tests against a real Backblaze B2 test bucket.

These are OPT-IN: they skip cleanly unless the B2_TEST_* environment
variables are set. Point them at a disposable test bucket only —
files are created under 'integration-tests/' and deleted afterward.

Required:
    B2_TEST_KEY_ID           Application key ID for the test account
    B2_TEST_APPLICATION_KEY  Application key (B2_TEST_APP_KEY also accepted)
    B2_TEST_BUCKET           Test bucket name

Optional:
    B2_TEST_S3_ENDPOINT      S3 endpoint (B2_TEST_ENDPOINT also accepted);
                             enables the S3 adapter tests. A bare hostname
                             like 's3.us-west-004.backblazeb2.com' is fine.

Run with (variables in e.g. ~/.api_keys):
    set -a; . ~/.api_keys; set +a
    python3 -m pytest test_b2_live.py -v
"""

import os
import sys
import uuid
import tempfile
import unittest

try:
    import requests  # noqa: F401
except ImportError:
    raise unittest.SkipTest("'requests' is not installed; live tests need real HTTP.")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from b2_storage_adapter import (
    B2NativeAdapter,
    B2S3Adapter,
    B2AdapterException,
    BOTO3_AVAILABLE,
)

KEY_ID = os.environ.get("B2_TEST_KEY_ID", "")
APPLICATION_KEY = (os.environ.get("B2_TEST_APPLICATION_KEY", "")
                   or os.environ.get("B2_TEST_APP_KEY", ""))
BUCKET = os.environ.get("B2_TEST_BUCKET", "")
S3_ENDPOINT = (os.environ.get("B2_TEST_S3_ENDPOINT", "")
               or os.environ.get("B2_TEST_ENDPOINT", ""))
if S3_ENDPOINT and not S3_ENDPOINT.startswith("http"):
    S3_ENDPOINT = "https://" + S3_ENDPOINT

CREDENTIALS_SET = bool(KEY_ID and APPLICATION_KEY and BUCKET)
TEST_PREFIX = "integration-tests/"


def make_payload_file():
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.write(b"\x89PNG-fake-payload-" + uuid.uuid4().bytes)
    tmp.close()
    return tmp.name


@unittest.skipUnless(CREDENTIALS_SET, "B2_TEST_KEY_ID / B2_TEST_APPLICATION_KEY / B2_TEST_BUCKET not set")
class TestLiveNativeAdapter(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.adapter = B2NativeAdapter(KEY_ID, APPLICATION_KEY)
        cls.adapter.authenticate()

    def setUp(self):
        self.local_path = make_payload_file()
        self.addCleanup(os.unlink, self.local_path)
        self.remote_name = f"{TEST_PREFIX}{uuid.uuid4().hex}.png"

    def tearDown(self):
        # Delete every uploaded version of the test file
        try:
            for f in self.adapter.list_files(BUCKET, prefix=self.remote_name):
                self.adapter._api_request(
                    "b2_delete_file_version",
                    {"fileName": f["name"], "fileId": f["id"]},
                )
        except B2AdapterException:
            pass

    def test_upload_list_download_roundtrip(self):
        url = self.adapter.upload_file(self.local_path, self.remote_name, BUCKET)
        self.assertTrue(url.startswith("https://"))

        listed = self.adapter.list_files(BUCKET, prefix=self.remote_name)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], self.remote_name)
        self.assertEqual(listed[0]["size"], os.path.getsize(self.local_path))

        download_path = self.local_path + ".downloaded"
        self.addCleanup(os.unlink, download_path)
        self.adapter.download_file(self.remote_name, download_path, BUCKET)
        with open(self.local_path, "rb") as a, open(download_path, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_upload_url_reused_for_second_file(self):
        second_remote = f"{TEST_PREFIX}{uuid.uuid4().hex}.png"
        self.adapter.upload_file(self.local_path, self.remote_name, BUCKET)
        bucket_id = self.adapter._get_bucket_id(BUCKET)
        cached_before = self.adapter._upload_url_cache.get(bucket_id)
        self.assertIsNotNone(cached_before)

        self.adapter.upload_file(self.local_path, second_remote, BUCKET)
        self.assertIs(self.adapter._upload_url_cache.get(bucket_id), cached_before)

        for f in self.adapter.list_files(BUCKET, prefix=second_remote):
            self.adapter._api_request(
                "b2_delete_file_version", {"fileName": f["name"], "fileId": f["id"]}
            )

    def test_wrong_bucket_gives_clear_error(self):
        with self.assertRaises(B2AdapterException):
            self.adapter.upload_file(
                self.local_path, self.remote_name, "no-such-bucket-" + uuid.uuid4().hex[:8]
            )


@unittest.skipUnless(CREDENTIALS_SET, "B2_TEST_KEY_ID / B2_TEST_APPLICATION_KEY / B2_TEST_BUCKET not set")
class TestLiveFaultInjection(unittest.TestCase):
    """
    B2 integration checklist: verify upload resilience using Backblaze's
    fault-injection header (X-Bz-Test-Mode: fail_some_uploads), which makes
    the real service intermittently fail uploads with 503.
    """

    UPLOAD_COUNT = 4

    def test_uploads_survive_injected_503_failures(self):
        import logging
        from unittest.mock import patch as mock_patch
        import b2_storage_adapter as mod

        adapter = B2NativeAdapter(KEY_ID, APPLICATION_KEY)
        adapter.authenticate()

        real_post = mod.requests.post

        def post_with_fault_injection(url, **kwargs):
            headers = dict(kwargs.get("headers") or {})
            headers["X-Bz-Test-Mode"] = "fail_some_uploads"
            kwargs["headers"] = headers
            return real_post(url, **kwargs)

        # Count the adapter's retry warnings so the test can report whether
        # faults were actually injected during this run.
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        mod.logger.addHandler(handler)
        self.addCleanup(mod.logger.removeHandler, handler)

        local_path = make_payload_file()
        self.addCleanup(os.unlink, local_path)
        uploaded = []
        exhaustions = 0
        try:
            with mock_patch.object(mod.requests, "post", side_effect=post_with_fault_injection):
                for i in range(self.UPLOAD_COUNT):
                    remote = f"{TEST_PREFIX}fault-injection-{uuid.uuid4().hex}.png"
                    try:
                        adapter.upload_file(local_path, remote, BUCKET)
                    except B2AdapterException as e:
                        # Sustained injection can legitimately exhaust the
                        # retry budget. The adapter's contract for that case
                        # is a clean failure after MAX_UPLOAD_ATTEMPTS —
                        # assert it (not just swallow it), count it, then
                        # give the file a fresh set of attempts.
                        self.assertIn(
                            f"after {mod.MAX_UPLOAD_ATTEMPTS} attempts", str(e),
                            "retry budget exhaustion must surface as the documented error",
                        )
                        exhaustions += 1
                        adapter.upload_file(local_path, remote, BUCKET)
                    uploaded.append(remote)

            listed = {f["name"] for f in adapter.list_files(BUCKET, prefix=f"{TEST_PREFIX}fault-injection-")}
            for remote in uploaded:
                self.assertIn(remote, listed)

            retries = sum(1 for r in records if r.levelno >= logging.WARNING)
            print(f"\n[fault-injection] {self.UPLOAD_COUNT} uploads OK, "
                  f"{retries} injected failure(s) retried, "
                  f"{exhaustions} retry-budget exhaustion(s) (verified clean)")
        finally:
            for remote in uploaded:
                try:
                    for f in adapter.list_files(BUCKET, prefix=remote):
                        adapter._api_request(
                            "b2_delete_file_version",
                            {"fileName": f["name"], "fileId": f["id"]},
                        )
                except B2AdapterException:
                    pass


@unittest.skipUnless(
    CREDENTIALS_SET and S3_ENDPOINT and BOTO3_AVAILABLE,
    "B2_TEST_S3_ENDPOINT not set (or boto3 not installed)",
)
class TestLiveS3Adapter(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.adapter = B2S3Adapter(KEY_ID, APPLICATION_KEY, S3_ENDPOINT)
        cls.adapter.authenticate()

    def setUp(self):
        self.local_path = make_payload_file()
        self.addCleanup(os.unlink, self.local_path)
        self.remote_name = f"{TEST_PREFIX}{uuid.uuid4().hex}.png"

    def tearDown(self):
        try:
            self.adapter.s3_client.delete_object(Bucket=BUCKET, Key=self.remote_name)
        except Exception:
            pass

    def test_upload_list_download_roundtrip(self):
        self.adapter.upload_file(self.local_path, self.remote_name, BUCKET)

        listed = self.adapter.list_files(BUCKET, prefix=self.remote_name)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], self.remote_name)

        download_path = self.local_path + ".downloaded"
        self.addCleanup(os.unlink, download_path)
        self.adapter.download_file(self.remote_name, download_path, BUCKET)
        with open(self.local_path, "rb") as a, open(download_path, "rb") as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
