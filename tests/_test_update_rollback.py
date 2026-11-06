"""Retained backups and rollback (issue #561 follow-up).

A rollback restores `updates/backup-<dir version>/`, whose *content* is the
version that ran before the update that folder was taken for. These tests pin
the three things that can silently break an install: picking a half-written
backup, restoring the wrong version, and leaving a gateway that will not start
without a way back.
"""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_settings as S
import wb_update_portable as U
import wb_update_request as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def proxy_source(version):
    return 'server_version = "wb-proxy/%s"\n' % version


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def make_install(d, version="2.0.0"):
    """A minimal app root that looks like an installed gateway."""
    root = os.path.join(d, "app")
    write(os.path.join(root, "wb_proxy.py"), proxy_source(version))
    write(os.path.join(root, "dashboard.html"), "<html>%s</html>" % version)
    write(os.path.join(root, "release-manifest.json"),
          json.dumps({"version": version, "root": ".",
                      "files": ["wb_proxy.py", "dashboard.html"]}))
    return root


def make_backup(root, dir_version, content_version, files=None, absent=None,
                manifest_files=None):
    """A backup folder as `backup_current()` writes it.

    `dir_version` is the folder suffix (the update it was taken for); the files
    inside carry `content_version`, the version a restore lands on.
    """
    files = files or ["wb_proxy.py", "dashboard.html"]
    absent = absent or []
    backup = os.path.join(root, "updates", U.BACKUP_PREFIX + dir_version)
    write(os.path.join(backup, "wb_proxy.py"), proxy_source(content_version))
    write(os.path.join(backup, "dashboard.html"), "<html>%s</html>" % content_version)
    for rel in absent:
        pass
    write(os.path.join(backup, "backup.json"),
          json.dumps({"version": dir_version,
                      "saved": [r for r in files if r not in absent],
                      "absent": absent}))
    write(os.path.join(backup, "release-manifest.json"),
          json.dumps({"files": manifest_files or files}))
    return backup


class KeepBackupsSettingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="keep-backups-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_default_is_one(self):
        self.assertEqual(S.keep_backups(self.dir), S.DEFAULT_KEEP_BACKUPS)
        self.assertEqual(S.DEFAULT_KEEP_BACKUPS, 1)

    def test_values_are_clamped_to_the_documented_range(self):
        self.assertEqual(S.set_keep_backups(self.dir, 0), 0)
        self.assertEqual(S.keep_backups(self.dir), 0)
        self.assertEqual(S.set_keep_backups(self.dir, 2), 2)
        self.assertEqual(S.set_keep_backups(self.dir, 9), S.MAX_KEEP_BACKUPS)
        self.assertEqual(S.MAX_KEEP_BACKUPS, 3)
        self.assertEqual(S.set_keep_backups(self.dir, -4), 0)

    def test_a_hand_edited_value_falls_back_to_the_default(self):
        write(os.path.join(self.dir, "settings.json"),
              json.dumps({"keep_backups": "many"}))
        self.assertEqual(S.keep_backups(self.dir), S.DEFAULT_KEEP_BACKUPS)
        write(os.path.join(self.dir, "settings.json"),
              json.dumps({"keep_backups": True}))
        self.assertEqual(S.keep_backups(self.dir), S.DEFAULT_KEEP_BACKUPS)


class BackupInspectionTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="backups-")
        self.root = make_install(self.d)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_a_complete_backup_reports_the_version_it_would_restore(self):
        make_backup(self.root, "2.0.0", "1.0.0")
        entries = U.list_backups(self.root)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["version"], "1.0.0")
        self.assertEqual(entries[0]["dir_version"], "2.0.0")
        self.assertTrue(entries[0]["complete"], entries[0]["reason"])
        self.assertEqual(entries[0]["files"], 2)

    def test_a_backup_missing_a_file_is_not_complete(self):
        backup = make_backup(self.root, "2.0.0", "1.0.0")
        os.remove(os.path.join(backup, "dashboard.html"))
        entries = U.list_backups(self.root)
        self.assertFalse(entries[0]["complete"])
        self.assertIn("dashboard.html", entries[0]["reason"])

    def test_a_backup_whose_metadata_disagrees_with_its_folder_is_rejected(self):
        backup = make_backup(self.root, "2.0.0", "1.0.0")
        write(os.path.join(backup, "backup.json"),
              json.dumps({"version": "9.9.9", "saved": ["wb_proxy.py",
                                                        "dashboard.html"],
                          "absent": []}))
        entries = U.list_backups(self.root)
        self.assertFalse(entries[0]["complete"])
        self.assertIn("目录名", entries[0]["reason"])

    def test_a_backup_without_a_readable_version_is_rejected(self):
        backup = make_backup(self.root, "2.0.0", "1.0.0")
        write(os.path.join(backup, "wb_proxy.py"), "# no version here\n")
        entries = U.list_backups(self.root)
        self.assertFalse(entries[0]["complete"])
        self.assertIn("没有可识别的版本", entries[0]["reason"])

    def test_a_file_count_that_disagrees_with_the_manifest_is_rejected(self):
        make_backup(self.root, "2.0.0", "1.0.0",
                    manifest_files=["wb_proxy.py", "dashboard.html", "extra.py"])
        entries = U.list_backups(self.root)
        self.assertFalse(entries[0]["complete"])
        self.assertIn("manifest", entries[0]["reason"])

    def test_find_backup_addresses_the_folder_by_content_version(self):
        make_backup(self.root, "2.0.0", "1.0.0")
        found = U.find_backup(self.root, "1.0.0")
        self.assertTrue(found and found.endswith(U.BACKUP_PREFIX + "2.0.0"))
        self.assertIsNone(U.find_backup(self.root, "3.0.0"))

    def test_an_incomplete_backup_is_never_found(self):
        backup = make_backup(self.root, "2.0.0", "1.0.0")
        os.remove(os.path.join(backup, "wb_proxy.py"))
        self.assertIsNone(U.find_backup(self.root, "1.0.0"))


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="prune-")
        self.root = make_install(self.d)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def _three(self):
        make_backup(self.root, "2.0.0", "1.0.0")
        make_backup(self.root, "3.0.0", "2.0.0")
        make_backup(self.root, "4.0.0", "3.0.0")
        base = os.path.join(self.root, "updates")
        now = time.time()
        for offset, name in enumerate(("backup-2.0.0", "backup-3.0.0",
                                       "backup-4.0.0")):
            os.utime(os.path.join(base, name), (now - 300 + offset * 100,
                                                now - 300 + offset * 100))

    def test_keep_one_drops_the_older_backups(self):
        self._three()
        removed = U.prune_backups(self.root, 1)
        self.assertEqual(sorted(removed), ["2.0.0", "3.0.0"])
        self.assertEqual([e["dir_version"] for e in U.list_backups(self.root)],
                         ["4.0.0"])

    def test_keep_zero_drops_everything(self):
        self._three()
        removed = U.prune_backups(self.root, 0)
        self.assertEqual(len(removed), 3)
        self.assertEqual(U.list_backups(self.root), [])

    def test_keep_two_keeps_the_newest_pair(self):
        self._three()
        U.prune_backups(self.root, 2)
        self.assertEqual([e["dir_version"] for e in U.list_backups(self.root)],
                         ["4.0.0", "3.0.0"])


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rollback-")
        self.root = make_install(self.d, "2.0.0")
        self.status = os.path.join(self.d, "status.json")
        self.backup = make_backup(self.root, "2.0.0", "1.0.0")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def _run(self, health=True, **kwargs):
        with mock.patch.object(U, "stop_gateway"), \
                mock.patch.object(U, "wait_port_free", return_value=True), \
                mock.patch.object(U, "is_visible_process", return_value=False), \
                mock.patch.object(U, "wait_health", return_value=health):
            return U.run_rollback(self.root, self.root, "1.0.0", 8788, 1234,
                                  status_file=self.status,
                                  start_service_fn=None, **kwargs)

    def _state(self):
        return json.loads(read(self.status))

    def test_a_rollback_restores_the_backed_up_version(self):
        self.assertEqual(self._run(), 0)
        self.assertIn("1.0.0", read(os.path.join(self.root, "wb_proxy.py")))
        self.assertEqual(self._state()["state"], "restored")
        self.assertEqual(self._state()["version"], "1.0.0")

    def test_a_successful_rollback_leaves_no_safety_copy(self):
        self._run()
        self.assertFalse(os.path.isdir(
            os.path.join(self.root, "updates", U.SAFETY_DIR)))

    def test_the_rollback_target_must_exist(self):
        self.assertEqual(self._run_version("3.0.0"), 1)
        self.assertEqual(self._state()["state"], "failed")
        self.assertIn("没有可回退", self._state()["message"])

    def _run_version(self, version):
        with mock.patch.object(U, "stop_gateway"), \
                mock.patch.object(U, "wait_port_free", return_value=True), \
                mock.patch.object(U, "wait_health", return_value=True):
            return U.run_rollback(self.root, self.root, version, 8788, 1234,
                                  status_file=self.status,
                                  start_service_fn=None)

    def test_a_gateway_that_will_not_serve_the_old_version_is_put_back(self):
        """The probe fails, so the live files go back to what they were."""
        calls = {"n": 0}

        def health(port, timeout=45, expect_version=None):
            calls["n"] += 1
            return expect_version is None

        with mock.patch.object(U, "stop_gateway"), \
                mock.patch.object(U, "wait_port_free", return_value=True), \
                mock.patch.object(U, "is_visible_process", return_value=False), \
                mock.patch.object(U, "wait_health", side_effect=health):
            code = U.run_rollback(self.root, self.root, "1.0.0", 8788, 1234,
                                  status_file=self.status,
                                  start_service_fn=None)
        self.assertEqual(code, 1)
        state = self._state()
        self.assertEqual(state["state"], "failed")
        self.assertIn("已恢复到回退前的版本", state["message"])
        self.assertIn("2.0.0", read(os.path.join(self.root, "wb_proxy.py")))
        self.assertGreaterEqual(calls["n"], 2)

    def test_rollback_cleans_the_download_folder_and_prunes(self):
        make_backup(self.root, "3.0.0", "2.0.0")
        write(os.path.join(self.root, "updates", "1.0.0", "workbuddy2api-hub-v1.0.0.zip"),
              "zip")
        self._run(keep_backups=1)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "updates", "1.0.0")))
        self.assertEqual(len(U.list_backups(self.root)), 1)


class WaitHealthVersionTests(unittest.TestCase):
    def _response(self, server):
        resp = mock.MagicMock()
        resp.status = 200
        resp.headers = {"Server": server}
        resp.__enter__ = lambda self: resp
        resp.__exit__ = lambda *args: False
        return resp

    def test_a_200_from_the_old_version_is_not_success(self):
        with mock.patch.object(U.urllib.request, "urlopen",
                               return_value=self._response(
                                   "wb-proxy/2.0.0 Python/3.11.17")), \
                mock.patch.object(U.time, "sleep"):
            self.assertFalse(U.wait_health(8788, timeout=0.1,
                                           expect_version="1.0.0"))

    def test_the_expected_version_is_success(self):
        with mock.patch.object(U.urllib.request, "urlopen",
                               return_value=self._response(
                                   "wb-proxy/1.0.0 Python/3.11.17")):
            self.assertTrue(U.wait_health(8788, timeout=5,
                                          expect_version="1.0.0"))

    def test_without_an_expectation_a_200_is_enough(self):
        with mock.patch.object(U.urllib.request, "urlopen",
                               return_value=self._response("")):
            self.assertTrue(U.wait_health(8788, timeout=5))


class PanelRequestTests(unittest.TestCase):
    """The request layer: what the panel may queue, and what it refuses."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rollback-panel-")
        self.request = os.path.join(self.d, "request.json")
        self.status = os.path.join(self.d, "status.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def _bridge(self, backups, external=True):
        """Patch the request layer into a Windows external-executor deployment."""
        return [
            mock.patch.object(R, "REQUEST_DIR", self.d),
            mock.patch.object(R, "REQUEST_FILE", self.request),
            mock.patch.object(R, "STATUS_FILE", self.status),
            mock.patch.object(R, "install_enabled", return_value=True),
            mock.patch.object(R, "external_enabled", return_value=external),
            mock.patch.object(R, "backups", return_value={
                "source": "local", "backups": backups}),
            mock.patch.object(R, "status", return_value={
                "install_enabled": True,
                "installation": {"state": "idle", "version": "", "message": ""}}),
        ]

    def test_a_rollback_request_carries_the_action_and_the_tag(self):
        patches = self._bridge([{"version": "1.0.0", "complete": True}])
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        queued = R.request_rollback("1.0.0", "2.0.0", port=8788)
        with open(self.request, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["version"], "1.0.0")
        self.assertEqual(saved["action"], "rollback")
        # The tag is what makes an executor that predates this feature treat
        # the same request as "reinstall that tag" instead of refusing it.
        self.assertEqual(saved["tag"], "v1.0.0")
        self.assertEqual(saved["version"], "1.0.0")

    def test_a_second_rollback_cannot_queue_while_one_is_pending(self):
        patches = self._bridge([{"version": "1.0.0", "complete": True}])
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        R.request_rollback("1.0.0", "2.0.0")
        with self.assertRaises(R.UpdateRequestError):
            R.request_rollback("1.0.0", "2.0.0")

    def test_a_rollback_needs_a_complete_backup_of_that_version(self):
        for backups in ([], [{"version": "1.0.0", "complete": False}]):
            patches = self._bridge(backups)
            for item in patches:
                item.start()
                self.addCleanup(item.stop)
            with self.subTest(backups=backups), \
                    self.assertRaises(R.UpdateRequestError) as caught:
                R.request_rollback("1.0.0", "2.0.0")
            self.assertIn("没有可回退", str(caught.exception))
            self.assertFalse(os.path.exists(self.request))

    def test_a_rollback_to_the_running_version_is_refused(self):
        patches = self._bridge([{"version": "2.0.0", "complete": True}])
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with self.assertRaises(R.UpdateRequestError) as caught:
            R.request_rollback("2.0.0", "2.0.0")
        self.assertIn("当前版本相同", str(caught.exception))
        self.assertFalse(os.path.exists(self.request))

    def test_the_target_must_be_a_plain_semver(self):
        patches = self._bridge([{"version": "1.0.0", "complete": True}])
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        for value in ("", "1.7", "1.7.24.1", "../1.7.24", "abc", "1.7.x"):
            with self.subTest(value=value), \
                    self.assertRaises(R.UpdateRequestError):
                R.request_rollback(value, "2.0.0")
        self.assertFalse(os.path.exists(self.request))

    def test_restore_states_are_visible_and_restoring_is_active(self):
        for module in (R, U):
            with self.subTest(module=module.__name__):
                self.assertIn("restoring", module.ACTIVE_STATES)
                self.assertIn("restoring", module.VISIBLE_STATES)
                self.assertIn("restored", module.VISIBLE_STATES)
                self.assertNotIn("restored", module.ACTIVE_STATES)

    def test_a_rollback_without_an_executor_is_refused(self):
        with mock.patch.object(R, "install_enabled", return_value=False):
            with self.assertRaises(R.UpdateRequestError) as caught:
                R.request_rollback("1.0.0", "2.0.0")
        self.assertIn("尚未配置", str(caught.exception))


class PortableRequestRollbackTests(unittest.TestCase):
    """The portable package queues its own detached helper instead."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="rollback-portable-")
        self.root = make_install(self.d, "2.0.0")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_the_helper_is_spawned_with_the_rollback_action(self):
        make_backup(self.root, "2.0.0", "1.0.0")
        spawned = []
        with mock.patch.object(U, "available", return_value=True), \
                mock.patch.object(U, "app_root", return_value=self.root), \
                mock.patch.object(U, "package_root", return_value=self.root), \
                mock.patch.object(U, "status", return_value={"state": "idle"}), \
                mock.patch.object(U, "_write_status"), \
                mock.patch.object(U, "_spawn_helper",
                                  side_effect=lambda cmd, *a: spawned.append(cmd)):
            queued = U.request_rollback("1.0.0", "2.0.0")
        self.assertEqual(queued["version"], "1.0.0")
        self.assertEqual(spawned[0][spawned[0].index("--action") + 1], "rollback")
        self.assertIn("1.0.0", spawned[0])
        self.assertNotIn("--tag", spawned[0])

    def test_a_missing_backup_is_refused_before_anything_is_spawned(self):
        spawned = []
        with mock.patch.object(U, "available", return_value=True), \
                mock.patch.object(U, "app_root", return_value=self.root), \
                mock.patch.object(U, "status", return_value={"state": "idle"}), \
                mock.patch.object(U, "_write_status"), \
                mock.patch.object(U, "_spawn_helper",
                                  side_effect=lambda cmd, *a: spawned.append(cmd)):
            with self.assertRaises(U.UpdateError) as caught:
                U.request_rollback("9.9.9", "2.0.0")
        self.assertIn("没有可回退", str(caught.exception))
        self.assertEqual(spawned, [])








class RetentionApplyTests(unittest.TestCase):
    """Changing the setting takes effect now, not at the next update."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="retention-")
        self.root = make_install(self.d, "3.0.0")
        make_backup(self.root, "2.0.0", "1.0.0")
        make_backup(self.root, "3.0.0", "2.0.0")
        base = os.path.join(self.root, "updates")
        now = time.time()
        os.utime(os.path.join(base, "backup-2.0.0"), (now - 200, now - 200))
        os.utime(os.path.join(base, "backup-3.0.0"), (now - 100, now - 100))
        # A failed rollback leaves this transient copy behind.
        write(os.path.join(base, U.SAFETY_DIR, "wb_proxy.py"), proxy_source("3.0.0"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def _apply(self, keep):
        with mock.patch.object(R, "external_enabled", return_value=False), \
                mock.patch.object(R, "__file__",
                                  os.path.join(self.root, "wb_update_request.py")):
            return R.apply_keep_backups(keep)

    def test_zero_drops_every_folder_and_the_stale_safety_copy(self):
        removed = self._apply(0)
        self.assertEqual(sorted(removed), ["2.0.0", "3.0.0"])
        self.assertEqual(U.list_backups(self.root), [])
        self.assertFalse(os.path.isdir(
            os.path.join(self.root, "updates", U.SAFETY_DIR)))

    def test_one_keeps_the_newest_and_still_drops_the_safety_copy(self):
        removed = self._apply(1)
        self.assertEqual(removed, ["2.0.0"])
        self.assertEqual([e["dir_version"] for e in U.list_backups(self.root)],
                         ["3.0.0"])
        self.assertFalse(os.path.isdir(
            os.path.join(self.root, "updates", U.SAFETY_DIR)))



if __name__ == "__main__":
    unittest.main()
