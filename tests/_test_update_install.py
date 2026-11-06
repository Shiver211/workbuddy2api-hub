"""Windows 更新面板的版本校验及请求排队。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_update_request


class RequestQueueTests(unittest.TestCase):
    def test_panel_request_writes_only_a_stable_newer_tag(self):
        with tempfile.TemporaryDirectory(prefix="wb-update-request-") as directory:
            request_path = os.path.join(directory, "request.json")
            status_path = os.path.join(directory, "status.json")
            with mock.patch.object(wb_update_request, "REQUEST_DIR", directory), \
                    mock.patch.object(wb_update_request, "REQUEST_FILE", request_path), \
                    mock.patch.object(wb_update_request, "STATUS_FILE", status_path), \
                    mock.patch.object(wb_update_request, "install_enabled", return_value=True), \
                    mock.patch.object(wb_update_request, "external_enabled", return_value=True):
                queued = wb_update_request.request_install("v1.7.13", "1.7.12")
                with open(request_path, "r", encoding="utf-8") as fh:
                    saved = json.load(fh)
                self.assertEqual(queued["state"], "queued")
                self.assertEqual(saved["tag"], "v1.7.13")
                self.assertEqual(saved["version"], "1.7.13")
                with self.assertRaises(wb_update_request.UpdateRequestError):
                    wb_update_request.request_install("v1.7.13", "1.7.12")

    def test_panel_request_rejects_disabled_or_non_newer_target(self):
        with tempfile.TemporaryDirectory(prefix="wb-update-request-refuse-") as directory:
            request_path = os.path.join(directory, "request.json")
            with mock.patch.object(wb_update_request, "REQUEST_DIR", directory), \
                    mock.patch.object(wb_update_request, "REQUEST_FILE", request_path), \
                    mock.patch.object(wb_update_request, "STATUS_FILE", os.path.join(directory, "status.json")), \
                    mock.patch.object(wb_update_request, "install_enabled", return_value=False):
                with self.assertRaises(wb_update_request.UpdateRequestError):
                    wb_update_request.request_install("v1.7.13", "1.7.12")
            with mock.patch.object(wb_update_request, "REQUEST_DIR", directory), \
                    mock.patch.object(wb_update_request, "REQUEST_FILE", request_path), \
                    mock.patch.object(wb_update_request, "STATUS_FILE", os.path.join(directory, "status.json")), \
                    mock.patch.object(wb_update_request, "install_enabled", return_value=True):
                for tag in ("v1.7.12", "v1.7.13-rc1", "not-a-tag"):
                    with self.subTest(tag=tag), self.assertRaises(wb_update_request.UpdateRequestError):
                        wb_update_request.request_install(tag, "1.7.13")
                self.assertFalse(os.path.exists(request_path))


if __name__ == "__main__":
    unittest.main()
