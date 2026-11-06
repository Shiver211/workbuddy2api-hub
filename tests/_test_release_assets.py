"""Windows 应用发布包：显式文件清单、可复现归档、校验和及草稿发布门禁。"""
import glob
import hashlib
import os
import re
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "release"))

import matrix_legs  # noqa: E402
import release_tools as tools  # noqa: E402

# The legs the repository has to keep running for a release today. The gate
# reads them from tests.yml rather than from this tuple - this is the
# expectation the reader checks that derivation against.
EXPECTED_LEGS = (
    "windows-latest / python 3.9",
    "windows-latest / python 3.12",
)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()




# ---- "only a tag push may write a release" --------------------------------
#
# A release mutation is a command the workflow actually *runs*: one that sits at
# command position, either on its own line inside a `run: |` block, after a
# one-line `run:`, or behind a shell separator. The preview job is allowed to
# *print* the same commands inside an echo, so matching the text anywhere would
# flag the wrong job - command position is what separates "runs it" from
# "talks about it".
# `gh api` is the subtle half. It defaults to GET, but *any* field flag turns
# the request into a POST, so `gh api repos/o/r/releases -f tag_name=v1` creates
# a release without ever naming a method. Only an explicit `-X GET` /
# `--method GET` says the call is a read.
#
# The scanner is deliberately conservative: it treats *every* writing `gh api`
# as a release writer rather than trying to recognise release URLs, because a
# miss means a release written from a manual run, while a false positive only
# means moving one command into the writer job.

# `gh release <verb>` - the four verbs that change a release.
GH_RELEASE_WRITE = re.compile(r"^gh\s+release\s+(?:create|edit|upload|delete)\b")
GH_API = re.compile(r"^gh\s+api\b")
# `-X POST`, `-XPOST`, `--method POST`, `--method=POST`.
API_METHOD = re.compile(r"(?:^|\s)(?:-X|--method)(?:[=\s]*)([A-Za-z]+)")
# Flags that make gh send a body, which is what flips the default to POST.
API_BODY_FLAG = re.compile(r"(?:^|\s)(?:-f|-F|--field|--raw-field|--input)(?:=|\s|$)")

# Fragments a shell command can be split into. Pipes are left alone on purpose:
# splitting on `|` would slice through quoted strings for no gain here.
SHELL_SEPARATOR = re.compile(r"&&|\|\||;")
# A one-line step carries its command after `run:` (possibly as a list item).
INLINE_RUN = re.compile(r"^(?:-\s*)?run:\s*")


def shell_commands(text):
    """Logical shell commands in a workflow, with `\\` continuations joined.

    Only line continuations are normalised - this is not a shell parser and
    does not try to be one.
    """
    joined, buf = [], ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        joined.append(buf + line)
        buf = ""
    if buf:
        joined.append(buf)

    for line in joined:
        for piece in SHELL_SEPARATOR.split(line):
            piece = INLINE_RUN.sub("", piece.strip())
            if piece:
                yield piece


def is_release_mutation(command):
    """Would running this command change a GitHub Release?"""
    if GH_RELEASE_WRITE.match(command):
        return True
    if not GH_API.match(command):
        return False
    method = API_METHOD.search(command)
    if method:
        return method.group(1).upper() != "GET"
    return bool(API_BODY_FLAG.search(command))


def release_mutations(text):
    """The release-mutating commands a piece of workflow text runs."""
    return [c for c in shell_commands(text) if is_release_mutation(c)]
# The one guard that makes a job unreachable from a manual run: a dispatch has
# neither this event nor a tag ref, so no input combination can satisfy it.
TAG_PUSH_GUARD = "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"


def workflow_jobs(text):
    """{job id: job text} for the top-level `jobs:` mapping of a workflow."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "jobs:":
            start = i
            break
    if start is None:
        return {}
    jobs, current, buf = {}, None, []
    for line in lines[start + 1:]:
        if not line.strip():
            if current:
                buf.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        if indent == 2 and not line.lstrip().startswith("#") and line.rstrip().endswith(":"):
            if current:
                jobs[current] = "\n".join(buf)
            current = line.strip()[:-1]
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current:
        jobs[current] = "\n".join(buf)
    return jobs


def job_guard(job_text):
    """The job-level `if:` value, or '' when the job has none."""
    header = []
    for line in job_text.splitlines():
        if line.strip() == "steps:":
            break
        header.append(line)
    match = re.search(r"(?m)^\s*if:\s*(.+?)\s*$", "\n".join(header))
    return match.group(1) if match else ""


def dispatch_input_names(text):
    """Input names declared under `on.workflow_dispatch.inputs`."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "workflow_dispatch:":
            continue
        parent = len(line) - len(line.lstrip())
        for j in range(i + 1, len(lines)):
            cur = lines[j]
            if not cur.strip() or cur.lstrip().startswith("#"):
                continue
            indent = len(cur) - len(cur.lstrip())
            if indent <= parent:
                break
            if cur.strip() != "inputs:":
                continue
            names = []
            for nxt in lines[j + 1:]:
                if not nxt.strip() or nxt.lstrip().startswith("#"):
                    continue
                nind = len(nxt) - len(nxt.lstrip())
                if nind <= indent:
                    break
                if nind == indent + 2 and nxt.strip().endswith(":"):
                    names.append(nxt.strip()[:-1])
            return names
    return []


def release_writer_jobs(text):
    """Jobs that run at least one release-mutating command."""
    return sorted(name for name, body in workflow_jobs(text).items()
                  if release_mutations(body))


def check_release_writers(text):
    """Raise AssertionError unless only a tag push can reach a release writer."""
    writers = release_writer_jobs(text)
    assert writers, "没找到任何 release 写入点——检查器本身失效了，不是工作流变干净了"
    for name in writers:
        guard = job_guard(workflow_jobs(text)[name])
        assert guard == TAG_PUSH_GUARD, (
            "job %s 会写 release，但它的 if: 不是 tag 推送（实际 %r）" % (name, guard))
    inputs = dispatch_input_names(text)
    assert inputs == ["tag"], (
        "workflow_dispatch 只能有 tag 一个输入，实际是 %s" % inputs)


class ManifestTests(unittest.TestCase):
    def test_every_listed_path_exists(self):
        for name in tools.read_manifest():
            self.assertTrue(os.path.isfile(os.path.join(ROOT, name)),
                            "清单里的 %s 不存在" % name)

    def test_every_module_is_listed(self):
        # The regression this list exists for: a new wb_*.py that nobody
        # remembered to add would otherwise be missing from the release, and
        # the launcher would fail with ModuleNotFoundError on a user's machine.
        on_disk = sorted(os.path.basename(p) for p in glob.glob(os.path.join(ROOT, "wb_*.py")))
        self.assertTrue(on_disk, "仓库里没有 wb_*.py？")
        listed = set(tools.read_manifest())
        self.assertEqual([n for n in on_disk if n not in listed], [])

    def test_launchers_and_dashboard_are_listed(self):
        listed = tools.read_manifest()
        for name in ("dashboard.html", "console_tail.py", "wb_proxy.py", "README.md", "LICENSE",
                     "start-wb-proxy.bat", "start-wb-proxy-lan.bat"):
            self.assertIn(name, listed)

    def test_every_launcher_script_is_listed(self):
        """启动脚本按前缀全量扫：漏登记一个就在这儿红。

        #285 的教训：portable.txt 里 start-wb-proxy-lan.bat 被 restart-wb-proxy.bat
        顶掉了，而新脚本恰恰要调用它——便携包少了这个文件，Windows 的 LAN 启动与
        重启都会失败，因此按前缀检查所有 Windows 启动入口。
        """
        listed = tools.read_manifest()
        for prefix in ("start-wb-proxy", "restart-wb-proxy", "allow-firewall"):
            for path in sorted(glob.glob(os.path.join(ROOT, prefix + "*"))):
                name = os.path.basename(path)
                if name.endswith(".bat"):
                    self.assertIn(name, listed, "便携包清单缺少 %s" % name)

    def test_runtime_data_files_are_listed(self):
        # wb_pricing._candidate_file() prefers pricing/pricing.json over the
        # copy inlined in the module, so a package without it silently ships
        # older prices than the release it came from.
        self.assertIn("pricing/pricing.json", tools.read_manifest())

    def test_entries_are_relative_and_inside_the_repo(self):
        for name in tools.read_manifest():
            self.assertFalse(os.path.isabs(name), name)
            self.assertNotIn("..", name.split("/"), name)
            self.assertNotIn("\\", name, name)

    def test_no_duplicates(self):
        listed = tools.read_manifest()
        self.assertEqual(len(listed), len(set(listed)))

    def test_user_data_directories_are_not_managed_files(self):
        # #29 replaces exactly the managed files and must leave accounts/ and
        # usage/ byte-for-byte alone, so no manifest entry may live inside them.
        for name in tools.read_manifest():
            for protected in tools.PROTECTED:
                self.assertFalse(name.startswith(protected),
                                 "%s 落在受保护目录 %s 里" % (name, protected))

    def test_development_only_files_stay_out(self):
        listed = tools.read_manifest()
        for name in ("tests/run_all.py", "release/release_tools.py"):
            self.assertNotIn(name, listed)


class VisibleLauncherTests(unittest.TestCase):
    """Issue #559: the silent default stays, the console entry is opt-in."""

    def test_wrappers_select_the_console_branch(self):
        for wrapper, target in (
                ("start-wb-proxy-visible.bat", "start-wb-proxy.bat"),
                ("start-wb-proxy-lan-visible.bat", "start-wb-proxy-lan.bat")):
            text = read(wrapper)
            self.assertIn('set "WB_LAUNCH_MODE=console"', text)
            self.assertIn('call "%~dp0' + target + '" %*', text)

    def test_main_launchers_check_the_mode_before_pythonw(self):
        for name in ("start-wb-proxy.bat", "start-wb-proxy-lan.bat"):
            text = read(name)
            mode = text.find('if /i "%WB_LAUNCH_MODE%"=="console" goto use_python')
            pythonw = text.find("prefer pythonw")
            self.assertGreaterEqual(mode, 0, name)
            self.assertGreater(pythonw, mode, name)




class PortableZipTests(unittest.TestCase):
    # The version comes from the checkout, not a literal: a release tool that
    # refused to build after a version bump would be a tool nobody trusts, and
    # a test pinned to yesterday's number would hide that.
    VERSION = tools.source_version(ROOT)
    TAG = "v" + VERSION

    def build(self, directory, name="out.zip", **kwargs):
        return tools.build_portable(self.VERSION, self.TAG,
                                    os.path.join(directory, name),
                                    source=ROOT, **kwargs)

    def test_archive_holds_only_manifest_files_and_marker(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, files = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
            root = tools.PACKAGE_ROOT
            self.assertEqual(sorted(n for n in names if not n.startswith(root + "/")),
                             [tools.MANIFEST_NAME])
            self.assertEqual(sorted(n[len(root) + 1:] for n in names if n.startswith(root + "/")
                                   ),
                             sorted(files))

    def test_the_archive_does_not_bundle_python(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, _ = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                self.assertFalse(any(name.startswith(tools.PACKAGE_ROOT + "/python/")
                                     for name in zf.namelist()))



    def test_embedded_manifest_describes_the_release(self):
        import json
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, files = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read(tools.MANIFEST_NAME).decode("utf-8"))
            self.assertEqual(manifest["files"], files)
            self.assertEqual(manifest["protected"], tools.PROTECTED)
            self.assertEqual(manifest["root"], tools.PACKAGE_ROOT)
            self.assertEqual(manifest["version"], tools.source_version(ROOT))
            self.assertEqual(manifest["tag"], self.TAG)
            self.assertNotIn("runtime", manifest)
            # 自定义 Windows 安装可只替换应用层。
            app = manifest["profiles"]["app"]
            self.assertIn("wb_proxy.py", app)
            self.assertIn("dashboard.html", app)
            self.assertNotIn("pricing/pricing.json", app)
            self.assertNotIn("start-wb-proxy.bat", app)
            self.assertTrue(set(app) <= set(files))

    def test_launchers_use_installed_python(self):
        for name in ("start-wb-proxy.bat", "start-wb-proxy-lan.bat"):
            bat = read(name)
            self.assertIn('set "VIEWERPY=python"', bat)
            self.assertIn('set "VIEWERPY=py"', bat)
            self.assertNotIn("bundled", bat)
            self.assertIn("Install Python 3.9+", bat)

    def test_archive_paths_are_safe(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            path, _ = self.build(directory)
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    self.assertFalse(name.startswith("/"), name)
                    self.assertNotIn("..", name.split("/"), name)

    def test_rebuild_is_byte_identical(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            first, _ = self.build(directory, "a.zip")
            second, _ = self.build(directory, "b.zip")
            with open(first, "rb") as fa, open(second, "rb") as fb:
                self.assertEqual(fa.read(), fb.read())

    def test_version_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="relzip-") as directory:
            with self.assertRaises(SystemExit):
                tools.build_portable("9.9.9", "v9.9.9",
                                     os.path.join(directory, "x.zip"), source=ROOT)


class ChecksumTests(unittest.TestCase):
    def test_covers_every_asset_once_in_sha256sum_format(self):
        with tempfile.TemporaryDirectory(prefix="relsum-") as directory:
            names = ["workbuddy2api-hub-v1.6.17.zip",]
            assets = []
            for i, name in enumerate(names):
                p = os.path.join(directory, name)
                with open(p, "wb") as fh:
                    fh.write(b"asset-%d" % i)
                assets.append(p)
            out = os.path.join(directory, "SHA256SUMS")
            lines = tools.write_checksums(assets, out)
            self.assertEqual(len(lines), len(names))
            for line, path in zip(lines, sorted(assets, key=os.path.basename)):
                digest, name = line.split("  ")
                self.assertEqual(len(digest), 64)
                self.assertEqual(name.strip(), os.path.basename(path))
                with open(path, "rb") as fh:
                    self.assertEqual(digest, hashlib.sha256(fh.read()).hexdigest())

    def test_hash_follows_the_bytes(self):
        with tempfile.TemporaryDirectory(prefix="relsum-") as directory:
            p = os.path.join(directory, "a.zip")
            with open(p, "wb") as fh:
                fh.write(b"one")
            before = tools.sha256(p)
            with open(p, "wb") as fh:
                fh.write(b"two")
            self.assertNotEqual(before, tools.sha256(p))


class ReleaseBodyTests(unittest.TestCase):
    block = None

    def setUp(self):
        names = tools.asset_names("1.6.17")
        self.block = tools.download_block("1.6.17", *names)

    def test_first_compose_has_the_marker_and_the_fixed_block(self):
        body = tools.compose_body("", self.block)
        self.assertIn(tools.MARKER, body)
        for expected in ("## 下载说明", "workbuddy2api-hub-v1.6.17.zip", "SHA256SUMS"):
            self.assertIn(expected, body)

    def test_maintainer_notes_above_the_marker_survive(self):
        notes = "## 本版要点\n\n- 修了一个真问题\n"
        body = tools.compose_body(notes + "\n" + tools.MARKER + "\n旧块\n", self.block)
        self.assertIn("- 修了一个真问题", body)
        self.assertNotIn("旧块", body)

    def test_recompose_is_idempotent(self):
        once = tools.compose_body("", self.block)
        twice = tools.compose_body(once, self.block)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(tools.MARKER), 1)


class MatrixLegsTests(unittest.TestCase):
    """The release gate names every leg instead of trusting a summary."""

    def test_the_repository_matrix_is_the_expected_two(self):
        self.assertEqual(tuple(matrix_legs.legs()), EXPECTED_LEGS)

    def test_dropping_a_leg_narrows_what_the_gate_requires(self):
        # Deriving the legs from tests.yml is the point: if a leg disappears
        # there, the gate stops waiting for it - and the unit above is what
        # makes that a deliberate change rather than a silent weakening.
        text = read(".github", "workflows", "tests.yml")
        without = text.replace('          - os: windows-latest\n            python: "3.12"\n', "")
        self.assertNotEqual(without, text, "tests.yml 里没有找到预期的 windows 腿")
        self.assertEqual(tuple(matrix_legs.legs(without)), EXPECTED_LEGS[:1])

    def test_an_unparseable_matrix_yields_nothing(self):
        # Empty means "the gate must refuse", never "no legs to check".
        self.assertEqual(matrix_legs.legs("name: tests\njobs:\n  test:\n    runs-on: x\n"), [])


class ReleaseToolingTests(unittest.TestCase):
    def test_the_tools_survive_a_non_utf8_console(self):
        # The Windows runner's stdout is cp1252, and the first Chinese progress
        # line aborted the first rehearsal of this workflow - the repository
        # already paid for the same lesson in tests/run_all.py. Both tools pin
        # their own streams instead of trusting the console.
        for name in ("release_tools.py",):
            text = read("release", name)
            self.assertIn('reconfigure(encoding="utf-8"', text, name)

    def test_the_workflow_pins_utf8_for_every_python_process(self):
        self.assertIn('PYTHONUTF8: "1"', read(".github", "workflows", "release.yml"))


class ReleaseMutationScannerTests(unittest.TestCase):
    """The command shapes the scanner has to classify, and how.

    Kept as a table so the contract is readable: everything in WRITES must be
    caught, everything in READS must not be. `gh api` is the interesting half -
    it defaults to GET, but any body flag turns the call into a POST.
    """

    WRITES = (
        "gh release create \"$TAG\" --draft",
        "gh release edit \"$TAG\" --draft",
        "gh release upload \"$TAG\" dist/*.zip --clobber",
        "gh release delete \"$TAG\" --yes",
        "gh api repos/o/r/releases -X POST -f tag_name=v1",
        "gh api -XPOST repos/o/r/releases",
        "gh api --method PATCH repos/o/r/releases/1",
        "gh api --method=DELETE repos/o/r/releases/1",
        "gh api repos/o/r/releases -f tag_name=v1",
        "gh api repos/o/r/releases -F tag_name=@body.json",
        "gh api repos/o/r/releases --field tag_name=v1",
        "gh api repos/o/r/releases --raw-field tag_name=v1",
        "gh api repos/o/r/releases --input body.json",
    )
    READS = (
        "gh release view \"$TAG\" --json body",
        "gh release list --limit 5",
        "gh api repos/o/r/releases",
        "gh api -X GET repos/o/r/releases -f per_page=1",
        "gh api --method=GET repos/o/r/releases",
        "gh api --method GET repos/o/r/releases/tags/$TAG",
        "echo \"gh release create $TAG --draft\"",
        "python release/release_tools.py checksums --out dist/SHA256SUMS",
        "git push --force-with-lease origin main",
    )

    def test_every_writing_shape_is_a_mutation(self):
        for command in self.WRITES:
            self.assertTrue(is_release_mutation(command), command)

    def test_every_read_only_shape_is_not_a_mutation(self):
        for command in self.READS:
            self.assertFalse(is_release_mutation(command), command)

    def test_continuations_are_joined_before_the_decision(self):
        text = ("        run: |\n"
                "          gh api \\\n"
                "            repos/o/r/releases \\\n"
                "            -f tag_name=v1\n")
        found = release_mutations(text)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].split(),
                         ["gh", "api", "repos/o/r/releases", "-f", "tag_name=v1"])

    def test_a_shell_separator_does_not_hide_a_command(self):
        text = "        run: true && gh release delete \"$TAG\" --yes\n"
        self.assertEqual(release_mutations(text), ["gh release delete \"$TAG\" --yes"])

    def test_an_echo_of_a_command_is_not_a_mutation(self):
        text = "        run: |\n          echo \"gh release create $TAG --draft\"\n"
        self.assertEqual(release_mutations(text), [])

    def test_the_real_workflow_only_writes_from_one_job(self):
        text = read(".github", "workflows", "release.yml")
        self.assertEqual(release_writer_jobs(text), ["write-draft"])
        self.assertTrue(release_mutations(workflow_jobs(text)["write-draft"]))


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = read(".github", "workflows", "release.yml")

    def test_triggers_on_version_tags(self):
        self.assertIn('tags: ["v*"]', self.text)

    def test_release_is_created_as_a_draft_and_never_published(self):
        self.assertIn("--draft", self.text)
        self.assertNotIn("--draft=false", self.text)
        # Every release mutation must stay on the draft side: `gh release edit`
        # without --draft, or `gh release create` without it, publishes.
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("gh release edit") or stripped.startswith("gh release create"):
                self.assertIn("--draft", stripped, stripped)

    def test_tag_must_match_the_source_version(self):
        # The gate reads both source strings, compares the tag against them and
        # ensures the package version agrees with its source.
        self.assertIn("github.ref_name", self.text)
        self.assertIn("server_version", self.text)
        self.assertIn('"version"', self.text)
        self.assertIn("TAGVER", self.text)

    def test_uses_the_manifest_tooling(self):
        self.assertIn("release/release_tools.py", self.text)
        self.assertIn("SHA256SUMS", self.text)

    def test_the_application_is_built_and_booted_on_windows(self):
        portable = self.text.split("  portable:", 1)[1].split("\n  draft-preview:", 1)[0]
        self.assertIn("runs-on: windows-latest", portable)
        self.assertIn("(Get-Command python).Source", portable)
        self.assertIn("/health", portable)
        self.assertIn("release-manifest.json", portable)
        self.assertNotIn("--runtime", portable)

    def test_every_release_writer_needs_the_full_matrix(self):
        # The gate derives the legs from tests.yml and checks each one by name.
        self.assertIn("release/matrix_legs.py", self.text)
        self.assertIn("matrix=full", self.text)
        self.assertIn("needs.gate.outputs.matrix", self.text)
        self.assertIn("writes == 'true'", self.text)
        # The authorisation has to come from the step that checks the legs; an
        # output wired to the wrong step is empty, and the writer then refuses
        # the mutation (which is how the first run caught this).
        gate = self.text.split("  gate:", 1)[1].split("\n  portable:", 1)[0]
        self.assertIn("matrix: ${{ steps.matrix.outputs.matrix }}", gate)
        self.assertIn("id: matrix", gate)

    # ---- only a tag push may write a release -------------------------------
    #
    # A manual run builds and hashes exactly what a tag push would, then stops.
    # The writer is a separate job whose `if:` a dispatch cannot satisfy, and
    # the checks below fail if that ever stops being true.

    def test_only_a_tag_push_can_reach_the_release_writer(self):
        check_release_writers(self.text)
        self.assertEqual(release_writer_jobs(self.text), ["write-draft"])

    def test_the_writer_guard_is_the_tag_push_condition(self):
        jobs = workflow_jobs(self.text)
        self.assertEqual(job_guard(jobs["write-draft"]), TAG_PUSH_GUARD)
        # The preview job runs for both events, so it must stay read-only.
        self.assertEqual(release_writer_jobs(self.text).count("draft-preview"), 0)
        self.assertIn("gh release view", jobs["draft-preview"])

    def test_dispatch_declares_no_input_that_could_authorise_a_write(self):
        self.assertEqual(dispatch_input_names(self.text), ["tag"])
        self.assertNotIn("inputs.dry_run", self.text)
        self.assertNotIn("dry_run", self.text)

    def test_the_checker_rejects_a_writer_moved_onto_the_dispatch_path(self):
        # Negative evidence, kept in the suite: a checker that cannot fail
        # proves nothing. Moving the writer onto the dispatch path has to be
        # caught, or the checks above are decoration.
        mutated = self.text.replace(TAG_PUSH_GUARD,
                                    "github.event_name == 'workflow_dispatch'")
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_a_mutation_hidden_in_another_job(self):
        mutated = self.text.replace(
            "      - name: Show what a tag push would do\n",
            "      - name: sneak a write in\n        run: |\n"
            "          gh release delete \"$TAG\" --yes\n"
            "      - name: Show what a tag push would do\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_a_one_line_run_step(self):
        # The short form is just as much a writer as the block form.
        mutated = self.text.replace(
            "      - name: Show what a tag push would do\n",
            "      - name: sneak a write in\n        run: gh release delete \"$TAG\" --yes\n"
            "      - name: Show what a tag push would do\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_a_dispatch_input_that_could_gate_a_write(self):
        mutated = self.text.replace(
            "    inputs:\n      tag:\n",
            "    inputs:\n      tag:\n      dry_run:\n        description: \"x\"\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_an_implicit_post_that_creates_a_release(self):
        # The round-3 scanner only knew explicit `-X POST|PATCH|PUT|DELETE`, so
        # this line created a release from a dispatch-reachable job without ever
        # naming a method: gh defaults to POST as soon as a field flag is there.
        # Committed on purpose - it is the regression this scanner exists for.
        mutated = self.text.replace(
            "      - name: Show what a tag push would do\n",
            "      - name: sneak a release in\n        run: |\n"
            "          gh api \"repos/$GH_REPO/releases\" -f tag_name=\"$TAG\" -f draft=true\n"
            "      - name: Show what a tag push would do\n", 1)
        self.assertNotEqual(mutated, self.text)
        self.assertIn("-f tag_name=", mutated)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_an_implicit_post_split_over_continuations(self):
        mutated = self.text.replace(
            "      - name: Show what a tag push would do\n",
            "      - name: sneak a release in\n        run: |\n"
            "          gh api \\\n"
            "            \"repos/$GH_REPO/releases\" \\\n"
            "            --field tag_name=\"$TAG\" \\\n"
            "            --raw-field draft=true\n"
            "      - name: Show what a tag push would do\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_the_checker_rejects_a_write_split_over_continuations(self):
        mutated = self.text.replace(
            "      - name: Show what a tag push would do\n",
            "      - name: sneak a write in\n        run: |\n"
            "          gh release \\\n            create \"$TAG\" --draft\n"
            "      - name: Show what a tag push would do\n", 1)
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_a_read_only_api_call_in_another_job_is_not_flagged(self):
        # The other half of the contract: an explicit GET stays legal anywhere,
        # even when it carries fields. A scanner that flagged this would push
        # people to weaken the check instead of fixing it.
        read_only = self.text.replace(
            "      - name: Show what a tag push would do\n",
            "      - name: peek at the release\n        run: |\n"
            "          gh api -X GET \"repos/$GH_REPO/releases\" -f per_page=1\n"
            "          gh api --method=GET \"repos/$GH_REPO/releases/tags/$TAG\"\n"
            "      - name: Show what a tag push would do\n", 1)
        self.assertNotEqual(read_only, self.text)
        check_release_writers(read_only)
        self.assertEqual(release_writer_jobs(read_only), ["write-draft"])

    def test_the_checker_notices_when_it_finds_no_writer_at_all(self):
        # If every writer disappears the checker must complain rather than pass
        # vacuously.
        mutated = self.text
        for verb in ("create", "edit", "upload"):
            mutated = mutated.replace("gh release %s" % verb, "true ")
        self.assertNotEqual(mutated, self.text)
        with self.assertRaises(AssertionError):
            check_release_writers(mutated)

    def test_a_reduced_run_only_ever_packages(self):
        # The single-OS suite may run for a packaging-only dry run and nowhere
        # else: that is what keeps it from authorising a release mutation.
        marker = "Run every suite (packaging-only dry run"
        self.assertIn(marker, self.text)
        block = self.text.split(marker, 1)[1].split("run:", 1)[0]
        self.assertIn("steps.decide.outputs.writes != 'true'", block)




    def test_portable_scripts_have_lf_and_executable_mode(self):
        # Issue #329: 检查便携包内脚本换行与执行权限
        # Windows 批处理必须为 CRLF；Python 源码必须为 LF。
        crlf = bytes([13, 10])
        with tempfile.TemporaryDirectory(prefix="wb-test-eol-") as tmp:
            out_zip = os.path.join(tmp, "out.zip")
            ver = tools.source_version(ROOT)
            tools.build_portable(ver, "v" + ver, out_zip, source=ROOT)
            with zipfile.ZipFile(out_zip, "r") as zf:
                for info in zf.infolist():
                    name_lower = info.filename.lower()
                    mode = (info.external_attr >> 16) & 0o777
                    if name_lower.endswith(".bat"):
                        content = zf.read(info)
                        self.assertIn(crlf, content, f"{info.filename} 必须包含 CRLF 换行符")
                        self.assertEqual(mode, 0o755, f"{info.filename} 权限不是 0o755")
                    elif name_lower.endswith(".py"):
                        content = zf.read(info)
                        self.assertNotIn(crlf, content, f"{info.filename} 必须为 LF 换行符")


if __name__ == "__main__":
    unittest.main()
