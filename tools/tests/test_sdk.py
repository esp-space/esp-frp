"""真实 Git 正反例验证唯一受管 SDK 派生；不访问设备或公开网络。"""
import copy
import importlib.util
import json
import os
import shutil
import shlex
import sys
import zlib
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("sdk", Path(__file__).parents[1] / "sdk.py")
SDK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SDK)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


class SDKContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "lwip-source"
        self.tlsf_source = self.root / "tlsf-source"
        self.sdk = self.root / "idf-source"
        self.recipe_source = self.root / "recipe-source"
        for path in (self.source, self.tlsf_source, self.sdk, self.recipe_source):
            path.mkdir()
            self.run_git(path, "init", "-q", "-b", "master")
            self.run_git(path, "config", "user.name", "SDK fixture")
            self.run_git(path, "config", "user.email", "sdk@example.invalid")
        (self.source / "tcp.c").write_text("official lwip\n")
        self.commit(self.source)
        self.original_lwip = self.run_git(self.source, "rev-parse", "HEAD")
        self.original = self.original_lwip
        (self.tlsf_source / "tlsf.c").write_text("official tlsf\n")
        self.commit(self.tlsf_source)
        for source, relative in ((self.source, "components/lwip/lwip"),
                                 (self.tlsf_source, "components/heap/tlsf")):
            self.run_git(self.sdk, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                         source.as_uri(), relative)
        (self.sdk / "sdk.c").write_text("official idf\n")
        self.commit(self.sdk)
        sdk_revision = self.run_git(self.sdk, "rev-parse", "HEAD")
        self.tlsf = self.sdk / "components/heap/tlsf"
        self.tlsf_revision = self.run_git(self.tlsf, "rev-parse", "HEAD")
        (self.source / "tcp.c").write_text("corrected lwip\n")
        self.commit(self.source)
        fixed = self.run_git(self.source, "rev-parse", "HEAD")
        self.lwip = self.sdk / "components/lwip/lwip"
        self.run_git(self.lwip, "fetch", "-q", "origin")
        self.run_git(self.lwip, "checkout", "-q", "--detach", fixed)
        (self.sdk / "sdk.c").write_text("official idf plus approved capacity statistics\n")
        (self.tlsf / "tlsf.c").write_text("official tlsf plus approved capacity statistics\n")
        self.recipe = {
            "schema_version": 2,
            "idf": {"repository": "https://github.com/darren-you/esp-idf.git", "revision": sdk_revision},
            "lwip": {"repository": "https://github.com/darren-you/esp-lwip.git", "revision": fixed,
                     "path": "components/lwip/lwip"},
            "tlsf": {"path": "components/heap/tlsf", "revision": self.tlsf_revision},
            "managed_patches": [],
            "derivation_stamp": SDK.DERIVATION_STAMP,
        }
        self.resources = []
        for name, repo, relative in (("idf", self.sdk, "sdk.c"), ("tlsf", self.tlsf, "tlsf.c")):
            content = self.run_git(repo, "diff", "--binary", "--", relative).encode() + b"\n"
            resource_path = f"tools/sdk-patches/capacity-{name}.patch"
            resource = self.recipe_source / resource_path
            resource.parent.mkdir(parents=True, exist_ok=True)
            resource.write_bytes(content)
            declaration = {"repository": name, "path": resource_path, "sha256": SDK.digest(content),
                           "files": [{"path": relative,
                                      "before_sha256": SDK.digest(SDK.git_bytes(repo, "HEAD:" + relative)),
                                      "after_sha256": SDK.digest((repo / relative).read_bytes())}]}
            self.recipe["managed_patches"].append(declaration)
            self.resources.append((declaration, resource))
        self.recipe_bytes = encoded(self.recipe)
        (self.recipe_source / "sdk-lock.json").write_bytes(self.recipe_bytes)
        self.commit(self.recipe_source)
        self.lock = {"schema_version": 2, "idf": self.recipe["idf"], "lwip": self.recipe["lwip"],
                     "sdk_derivation": {"repository": SDK.RECIPE_REPOSITORY,
                                        "revision": self.run_git(self.recipe_source, "rev-parse", "HEAD"),
                                        "path": "sdk-lock.json", "sha256": SDK.digest(self.recipe_bytes)}}
        self.stamp = self.sdk / SDK.DERIVATION_STAMP
        self.stamp.write_bytes(self.recipe_bytes)
        self.stamp.chmod(0o400)

    def run_git(self, path, *args):
        result = subprocess.run(["git", "-C", str(path), *args], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.rstrip("\n")

    def commit(self, path):
        self.run_git(path, "add", ".")
        self.run_git(path, "commit", "-q", "-m", "测试快照")

    def verify(self):
        SDK.verify(self.sdk, self.lock)

    def reject(self, message=None):
        with self.assertRaises(ValueError) if message is None else self.assertRaisesRegex(ValueError, message):
            self.verify()

    def replace_recipe(self, recipe):
        data = encoded(recipe)
        self.lock["sdk_derivation"]["sha256"] = SDK.digest(data)
        self.stamp.unlink(missing_ok=True)
        self.stamp.write_bytes(data)
        self.stamp.chmod(0o400)
        return data

    def pristine(self):
        self.run_git(self.sdk, "restore", "--", "sdk.c")
        self.run_git(self.tlsf, "restore", "--", "tlsf.c")
        self.stamp.unlink()

    def test_accepts_exact_derivation_and_locked_gitlink(self):
        self.verify()

    def test_check_never_fetches_or_executes_sdk_code(self):
        original = SDK.git
        def reads_only(path, *args):
            self.assertNotIn("fetch", args)
            self.assertNotIn("update", args)
            self.assertNotIn("apply", args)
            return original(path, *args)
        with patch.object(SDK, "git", reads_only), patch.object(SDK, "fetch_recipe", side_effect=AssertionError):
            self.verify()

    def test_rejects_original_lwip(self):
        self.run_git(self.lwip, "checkout", "-q", "--detach", self.original_lwip)
        self.reject("零窗口修正")

    def test_rejects_dirty_lwip(self):
        (self.lwip / "tcp.c").write_text("unverified\n")
        self.reject("未提交")

    def test_rejects_extra_sdk_change(self):
        (self.sdk / "other.c").write_text("unverified\n")
        self.reject("其他修改")

    def test_rejects_staged_sdk_change(self):
        self.run_git(self.sdk, "add", "sdk.c")
        self.reject("索引")

    def test_rejects_staged_tlsf_change(self):
        self.run_git(self.tlsf, "add", "tlsf.c")
        self.reject("索引")

    def test_rejects_untracked_tlsf_content(self):
        (self.tlsf / "other.c").write_text("unverified\n")
        self.reject("其他修改")

    def test_rejects_wrong_sdk_revision(self):
        self.lock["idf"] = dict(self.lock["idf"], revision="0" * 40)
        self.recipe["idf"] = self.lock["idf"]
        self.replace_recipe(self.recipe)
        self.reject("ESP-IDF")

    def test_rejects_wrong_tlsf_revision(self):
        self.recipe["tlsf"]["revision"] = "0" * 40
        self.replace_recipe(self.recipe)
        self.reject("TLSF")

    def test_rejects_missing_or_pristine_stamp(self):
        self.stamp.unlink()
        self.reject("普通文件")
        self.pristine_without_stamp()
        self.reject("普通文件")

    def pristine_without_stamp(self):
        self.run_git(self.sdk, "restore", "--", "sdk.c")
        self.run_git(self.tlsf, "restore", "--", "tlsf.c")

    def test_rejects_tampered_stamp(self):
        self.stamp.unlink(missing_ok=True)
        self.stamp.write_bytes(self.recipe_bytes + b"\n")
        self.stamp.chmod(0o400)
        self.reject("清单摘要")

    def test_rejects_stamp_symlink(self):
        original = self.root / "same-recipe.json"
        original.write_bytes(self.recipe_bytes)
        self.stamp.unlink(); self.stamp.symlink_to(original)
        self.reject("符号链接")

    def test_rejects_source_symlink_even_when_bytes_match(self):
        file = self.tlsf / "tlsf.c"
        source = self.root / "same-source.c"
        source.write_bytes(file.read_bytes())
        file.unlink(); file.symlink_to(source)
        # Git detects file-to-symlink type changes before the byte reader.
        self.reject()

    def test_rejects_partial_and_tampered_source(self):
        self.run_git(self.sdk, "restore", "--", "sdk.c")
        self.reject("精确派生摘要")
        (self.sdk / "sdk.c").write_text("tampered\n")
        self.reject("派生摘要")

    def test_rejects_forged_official_before_hash(self):
        self.recipe["managed_patches"][0]["files"][0]["before_sha256"] = "0" * 64
        self.replace_recipe(self.recipe)
        self.reject("before 摘要")

    def test_rejects_other_submodule_version_drift(self):
        (self.tlsf / "tlsf.c").write_text("different committed allocator\n")
        self.commit(self.tlsf)
        self.reject("TLSF")

    def test_rejects_uninitialized_tlsf(self):
        self.run_git(self.sdk, "submodule", "deinit", "-f", "components/heap/tlsf")
        self.reject("TLSF")

    def test_rejects_unknown_recipe_fields_and_duplicate_json(self):
        self.recipe["unknown_allowlist"] = ["anything"]
        self.replace_recipe(self.recipe)
        self.reject("字段")
        data = self.recipe_bytes.replace(b'{\n', b'{\n  "schema_version": 2,\n', 1)
        self.lock["sdk_derivation"]["sha256"] = SDK.digest(data)
        self.stamp.unlink(missing_ok=True)
        self.stamp.write_bytes(data)
        self.stamp.chmod(0o400)
        self.reject("重复字段")

    def test_rejects_duplicate_patch_or_path_escape(self):
        self.recipe["managed_patches"][1]["repository"] = "idf"
        self.replace_recipe(self.recipe)
        self.reject("修改仓库")
        self.recipe["managed_patches"][1]["repository"] = "tlsf"
        self.recipe["managed_patches"][0]["files"][0]["path"] = "../escaped.c"
        self.replace_recipe(self.recipe)
        self.reject("相对路径")

    def test_prepare_never_overwrites_existing_path(self):
        before = (self.sdk / "sdk.c").read_bytes()
        with self.assertRaisesRegex(ValueError, "输出路径已存在"):
            SDK.prepare(self.sdk, self.lock)
        self.assertEqual((self.sdk / "sdk.c").read_bytes(), before)

    def test_prepare_rejects_dangling_output_symlink(self):
        output = self.root / "dangling"
        output.symlink_to(self.root / "absent")
        with self.assertRaisesRegex(ValueError, "输出路径已存在"):
            SDK.prepare(output, self.lock)
        self.assertTrue(output.is_symlink())

    def test_apply_exact_recipe_from_pristine_real_git(self):
        self.pristine()
        SDK.apply_recipe(self.sdk, self.lock, self.recipe_bytes, self.recipe, self.resources)
        self.verify()
        self.assertEqual(self.stamp.read_bytes(), self.recipe_bytes)
        self.assertEqual(self.stamp.stat().st_mode & 0o777, 0o400)

    def test_apply_checks_every_patch_before_any_mutation(self):
        self.pristine()
        resource = self.resources[1][1]
        resource.write_bytes(resource.read_bytes() + b"corrupt\n")
        before = (self.sdk / "sdk.c").read_bytes()
        with self.assertRaisesRegex(ValueError, "输入摘要"):
            SDK.apply_recipe(self.sdk, self.lock, self.recipe_bytes, self.recipe, self.resources)
        self.assertEqual((self.sdk / "sdk.c").read_bytes(), before)
        self.assertFalse(self.stamp.exists())

    def test_apply_rejects_recipe_hash_before_mutation(self):
        self.pristine()
        before = (self.sdk / "sdk.c").read_bytes()
        with self.assertRaisesRegex(ValueError, "清单摘要"):
            SDK.apply_recipe(self.sdk, self.lock, self.recipe_bytes + b"\n", self.recipe, self.resources)
        self.assertEqual((self.sdk / "sdk.c").read_bytes(), before)
        self.assertFalse(self.stamp.exists())

    def test_apply_rejects_incomplete_resource_set_before_mutation(self):
        self.pristine()
        before = (self.sdk / "sdk.c").read_bytes()
        with self.assertRaisesRegex(ValueError, "输入集合"):
            SDK.apply_recipe(self.sdk, self.lock, self.recipe_bytes, self.recipe, self.resources[:1])
        self.assertEqual((self.sdk / "sdk.c").read_bytes(), before)
        self.assertFalse(self.stamp.exists())

    def test_recipe_lock_rejects_old_schema_or_other_source(self):
        path = self.root / "component-lock.json"
        path.write_bytes(encoded(self.lock))
        with patch.object(SDK, "LOCK_PATH", path):
            self.assertEqual(SDK.read_lock(), self.lock)
            invalid = dict(self.lock, schema_version=1)
            path.write_bytes(encoded(invalid))
            with self.assertRaisesRegex(ValueError, "旧 SDK 锁"):
                SDK.read_lock()
            invalid = copy.deepcopy(self.lock)
            invalid["sdk_derivation"]["repository"] = "https://github.com/example/other.git"
            path.write_bytes(encoded(invalid))
            with self.assertRaisesRegex(ValueError, "唯一 recipe"):
                SDK.read_lock()

    def test_full_prepare_uses_exact_git_sources_and_never_runs_base(self):
        output = self.root / "new-sdk"
        original = SDK.git
        sources = {self.lock["idf"]["repository"]: str(self.sdk),
                   self.lock["lwip"]["repository"]: str(self.source),
                   self.lock["sdk_derivation"]["repository"]: str(self.recipe_source)}
        fetches = []
        def local_git(path, *args):
            items = list(args)
            if items[:2] in (["remote", "add"], ["remote", "set-url"]):
                if items[-1] in sources:
                    items[-1] = sources[items[-1]]
            if items and items[0] == "submodule" and "update" in items:
                items = ["-c", "protocol.file.allow=always", *items]
            if items and items[0] == "fetch":
                fetches.append(items[-1])
            self.assertNotIn("submodule", items if path.name == "recipe" else [])
            return original(path, *items)
        with patch.object(SDK, "git", local_git):
            SDK.prepare(output, self.lock)
            SDK.verify(output, self.lock)
        self.assertEqual(fetches, [self.lock["sdk_derivation"]["revision"],
                                   self.lock["idf"]["revision"], self.lock["lwip"]["revision"]])
        self.assertEqual((output / SDK.DERIVATION_STAMP).read_bytes(), self.recipe_bytes)
        self.assertFalse((output / "tools/sdk-patches").exists())
        self.assertFalse((output / "tools/prepare_sdk.py").exists())


    def test_accepts_only_locked_gitlink(self):
        SDK.verify(self.sdk, self.lock)


    def test_rejects_untracked_sdk_content(self):
        (self.sdk / "other.c").write_text("unverified\n")
        with self.assertRaisesRegex(ValueError, "其他修改"):
            SDK.verify(self.sdk, self.lock)


    def test_rejects_shallow_nested_source(self):
        self.run_git(self.lwip, "fetch", "-q", "--depth=1",
                     self.source.as_uri(), self.lock["lwip"]["revision"])
        self.assertEqual(self.run_git(self.lwip, "rev-parse", "--is-shallow-repository"), "true")
        with self.assertRaisesRegex(ValueError, "shallow"):
            SDK.verify(self.sdk, self.lock)


    def test_rejects_partial_nested_source(self):
        self.run_git(self.lwip, "config", "remote.origin.promisor", "true")
        with self.assertRaisesRegex(ValueError, "partial"):
            SDK.verify(self.sdk, self.lock)


    def test_rejects_sparse_nested_source(self):
        self.run_git(self.lwip, "config", "core.sparseCheckout", "true")
        with self.assertRaisesRegex(ValueError, "sparse"):
            SDK.verify(self.sdk, self.lock)


    def test_rejects_sparse_worktree_configuration(self):
        self.run_git(self.lwip, "config", "extensions.worktreeConfig", "true")
        self.run_git(self.lwip, "config", "--worktree", "core.sparseCheckout", "true")
        with self.assertRaisesRegex(ValueError, "sparse"):
            SDK.verify(self.sdk, self.lock)


    def test_rejects_missing_history_object(self):
        relative = "objects/" + self.original[:2] + "/" + self.original[2:]
        object_path = Path(self.run_git(self.lwip, "rev-parse", "--git-path", relative))
        if not object_path.is_absolute():
            object_path = self.lwip / object_path
        # file:// clone stores real history in packs; unpack it before removing the original commit.
        for pack in sorted((object_path.parent.parent / "pack").glob("*.pack")):
            data = pack.read_bytes()
            pack.rename(self.root / pack.name)
            pack.with_suffix(".idx").unlink()
            subprocess.run(["git", "-C", str(self.lwip), "unpack-objects"],
                           input=data, check=True, capture_output=True)
        object_path.unlink()
        with self.assertRaisesRegex(ValueError, "对象不完整"):
            SDK.verify_complete_repository(self.lwip, source_root=self.sdk)


    def test_rejects_shared_nested_source_even_when_fsck_passes(self):
        shutil.rmtree(self.lwip)
        self.run_git(self.root, "clone", "-q", "--shared", str(self.source), str(self.lwip))
        self.run_git(self.lwip, "fsck", "--connectivity-only", "--no-dangling")
        with self.assertRaisesRegex(ValueError, "alternates"):
            SDK.verify(self.sdk, self.lock)


    def test_rejects_symlinked_git_directory_even_when_fsck_passes(self):
        alias = self.root / "symlinked-git-directory"
        shutil.copytree(self.source, alias, ignore=shutil.ignore_patterns(".git"))
        (alias / ".git").symlink_to(self.source / ".git", target_is_directory=True)
        self.run_git(alias, "fsck", "--connectivity-only", "--no-dangling")
        with self.assertRaisesRegex(ValueError, "Git 元数据"):
            SDK.verify_complete_repository(alias)


    def test_rejects_unbound_gitfile_even_when_fsck_passes(self):
        alias = self.root / "unbound-gitfile"
        shutil.copytree(self.source, alias, ignore=shutil.ignore_patterns(".git"))
        (alias / ".git").write_text("gitdir: " + str(self.source / ".git") + "\n")
        self.run_git(alias, "fsck", "--connectivity-only", "--no-dangling")
        with self.assertRaisesRegex(ValueError, "Git 元数据"):
            SDK.verify_complete_repository(alias)


    def test_rejects_linked_worktree_even_when_fsck_passes(self):
        linked = self.root / "linked"
        self.run_git(self.source, "worktree", "add", "-q", "--detach", str(linked), "HEAD")
        try:
            self.run_git(linked, "fsck", "--connectivity-only", "--no-dangling")
            with self.assertRaisesRegex(ValueError, "linked"):
                SDK.verify_complete_repository(linked)
        finally:
            self.run_git(self.source, "worktree", "remove", str(linked))


    def test_rejects_symlinked_object_storage_even_when_fsck_passes(self):
        objects = self.source / ".git/objects"
        outside = self.root / "outside-objects"
        objects.rename(outside)
        objects.symlink_to(outside, target_is_directory=True)
        self.run_git(self.source, "fsck", "--connectivity-only", "--no-dangling")
        with self.assertRaisesRegex(ValueError, "对象库"):
            SDK.verify_complete_repository(self.source)


    def test_rejects_environment_alternate_objects(self):
        with patch.dict(os.environ, {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.source / '.git/objects')}):
            with self.assertRaisesRegex(ValueError, "alternate"):
                SDK.verify(self.sdk, self.lock)


    def test_rejects_environment_object_directory(self):
        with patch.dict(os.environ, {"GIT_OBJECT_DIRECTORY": str(self.source / '.git/objects')}):
            with self.assertRaisesRegex(ValueError, "alternate"):
                SDK.verify_complete_repository(self.sdk)


    def test_rejects_external_separate_git_directory_with_worktree_binding(self):
        separate = self.root / "separate"
        outside = self.root / "separate.git"
        self.run_git(self.root, "clone", "-q", "--separate-git-dir=" + str(outside),
                 str(self.source), str(separate))
        self.run_git(separate, "config", "core.worktree", str(separate))
        self.assertEqual(self.run_git(separate, "status", "--porcelain"), "")
        self.run_git(separate, "fsck", "--connectivity-only", "--no-dangling")
        with self.assertRaisesRegex(ValueError, "Git 元数据"):
            SDK.verify_complete_repository(separate)


    def test_rejects_git_environment_redirecting_metadata_and_worktree(self):
        alias = self.root / "environment-redirect"
        shutil.copytree(self.source, alias, ignore=shutil.ignore_patterns(".git"))
        with patch.dict(os.environ, {"GIT_DIR": str(self.source / ".git"),
                                    "GIT_WORK_TREE": str(alias)}):
            with self.assertRaisesRegex(ValueError, "Git 环境"):
                SDK.verify_complete_repository(alias)


    def test_rejects_replace_ref_even_when_head_and_status_match(self):
        original = self.run_git(self.source, "rev-parse", "HEAD")
        filename = self.source / "tcp.c"
        original_bytes = filename.read_bytes()
        filename.write_text("int substituted_business;\n")
        self.run_git(self.source, "add", filename.name)
        self.run_git(self.source, "commit", "-qm", "different source fixture")
        replacement = self.run_git(self.source, "rev-parse", "HEAD")
        self.run_git(self.source, "replace", original, replacement)
        self.run_git(self.source, "checkout", "-q", "--detach", original)
        self.assertEqual(self.run_git(self.source, "rev-parse", "HEAD"), original)
        self.assertEqual(self.run_git(self.source, "status", "--porcelain"), "")
        self.assertEqual(filename.read_text(), "int substituted_business;\n")
        # The verifier's ordinary Git reads ignore replacement refs even before rejection.
        self.assertEqual(SDK.git(self.source, "show", "HEAD:" + filename.name).encode(),
                         original_bytes.rstrip(b"\n"))
        with self.assertRaisesRegex(ValueError, "replace"):
            SDK.verify_complete_repository(self.source)


    def test_rejects_grafts_even_when_fsck_passes(self):
        head = self.run_git(self.source, "rev-parse", "HEAD")
        (self.source / ".git/info/grafts").write_text(head + "\n")
        self.run_git(self.source, "fsck", "--connectivity-only", "--no-dangling")
        with self.assertRaisesRegex(ValueError, "grafts"):
            SDK.verify_complete_repository(self.source)


    def test_rejects_absorbed_submodule_metadata_outside_sdk_modules(self):
        directory = Path(self.run_git(self.lwip, "rev-parse", "--absolute-git-dir"))
        outside = self.root / "outside-lwip.git"
        directory.rename(outside)
        (self.lwip / ".git").write_text("gitdir: " + str(outside) + "\n")
        self.run_git(self.root, "config", "--file", str(outside / "config"), "core.worktree", str(self.lwip))
        self.run_git(self.lwip, "fsck", "--connectivity-only", "--no-dangling")
        with self.assertRaisesRegex(ValueError, "Git 元数据"):
            SDK.verify(self.sdk, self.lock)


    def test_accepts_direct_submodule_with_complete_self_owned_metadata(self):
        shutil.rmtree(self.lwip)
        self.run_git(self.root, "clone", "-q", str(self.source), str(self.lwip))
        self.run_git(self.lwip, "checkout", "-q", "--detach", self.lock["lwip"]["revision"])
        self.assertTrue((self.lwip / ".git").is_dir())
        SDK.verify(self.sdk, self.lock)


class SourceIntegrityTest(unittest.TestCase):
    """真实磁盘字节、文件模式与元数据不能由 Git 的干净状态替代。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self._git("init", "-q", "-b", "master")
        self._git("config", "user.name", "来源完整性 fixture")
        self._git("config", "user.email", "source@example.invalid")
        self.good = b"int good;\n"
        self.evil = b"int evil;\n"
        self.filename = self.source / "source.c"
        self.filename.write_bytes(self.good)
        self.filename.chmod(0o644)
        self._commit()
        self._verify()

    def _run_git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.source), *args], check=check,
                              capture_output=True, env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"})

    def _git(self, *args):
        return self._run_git(*args).stdout.decode().strip()

    def _commit(self):
        self._git("add", ".")
        self._git("commit", "-qm", "真实来源 fixture")
        self.head = self._git("rev-parse", "HEAD")

    def _verify(self):
        SDK.verify_complete_repository(self.source)

    def _assert_rejected(self, *, metadata=False):
        if metadata:
            with self.assertRaisesRegex(ValueError, "Git 元数据"):
                self._verify()
        else:
            with self.assertRaises((ValueError, RuntimeError, subprocess.SubprocessError)):
                self._verify()

    def _assert_hidden_byte_divergence(self):
        self.assertEqual(self._git("rev-parse", "HEAD"), self.head)
        self.assertEqual(self._git("status", "--porcelain", "--untracked-files=normal",
                                   "--ignore-submodules=none"), "")
        self.assertEqual(self._run_git("cat-file", "blob", "HEAD:source.c").stdout, self.good)
        self.assertEqual(self.filename.read_bytes(), self.evil)
        self.assertEqual(self._run_git("diff", "--exit-code", "HEAD").returncode, 0)
        self._git("fsck", "--connectivity-only", "--no-dangling")

    def test_rejects_clean_filter_hiding_raw_source_change(self):
        command = shlex.join([sys.executable, "-c",
                              'import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b"int good;\\n")'])
        self._git("config", "filter.fixture.clean", command)
        self._git("config", "filter.fixture.smudge", "cat")
        (self.source / ".git/info/attributes").write_text("*.c filter=fixture\n")
        self.filename.write_bytes(self.evil)
        self._assert_hidden_byte_divergence()
        self.assertEqual(self._git("hash-object", "--path=source.c", "source.c"),
                         self._git("rev-parse", "HEAD:source.c"))
        self.assertNotEqual(self._git("hash-object", "--no-filters", "source.c"),
                            self._git("rev-parse", "HEAD:source.c"))
        self._assert_rejected()

    def test_rejects_skip_worktree_hiding_raw_source_change(self):
        self._git("update-index", "--skip-worktree", "source.c")
        self.filename.write_bytes(self.evil)
        self._assert_hidden_byte_divergence()
        self.assertEqual(self._git("ls-files", "-v", "--", "source.c"), "S source.c")
        self._assert_rejected()

    def test_rejects_assume_unchanged_hiding_raw_source_change(self):
        self._git("update-index", "--assume-unchanged", "source.c")
        self.filename.write_bytes(self.evil)
        self._assert_hidden_byte_divergence()
        self.assertEqual(self._git("ls-files", "-v", "--", "source.c"), "h source.c")
        self._assert_rejected()

    def test_rejects_executable_change_hidden_by_core_filemode(self):
        self.assertTrue(self._git("ls-tree", "HEAD", "--", "source.c").startswith("100644 "))
        self._git("config", "core.filemode", "false")
        self.filename.chmod(0o755)
        self.assertEqual(self._git("status", "--porcelain"), "")
        self.assertEqual(self.filename.read_bytes(), self.good)
        self.assertEqual(self._run_git("cat-file", "blob", "HEAD:source.c").stdout, self.good)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.head)
        self.assertNotEqual(self.filename.stat().st_mode & 0o111, 0)
        self._assert_rejected()

    def _tracked_symlink(self):
        link = self.source / "link.c"
        link.symlink_to("source.c")
        self._commit()
        self.assertTrue(self._git("ls-tree", "HEAD", "--", "link.c").startswith("120000 "))
        self.assertEqual(self._run_git("cat-file", "blob", "HEAD:link.c").stdout, b"source.c")
        self._verify()
        self._git("update-index", "--skip-worktree", "link.c")
        return link

    def test_rejects_tracked_symlink_replaced_with_regular_file(self):
        link = self._tracked_symlink()
        link.unlink()
        link.write_bytes(b"source.c")
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_bytes(), self._run_git("cat-file", "blob", "HEAD:link.c").stdout)
        self.assertEqual(self._git("status", "--porcelain"), "")
        self._assert_rejected()

    def test_rejects_tracked_symlink_target_change(self):
        link = self._tracked_symlink()
        (self.root / "other.c").write_bytes(self.evil)
        link.unlink()
        link.symlink_to("../other.c")
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../other.c")
        self.assertEqual(link.read_bytes(), self.evil)
        self.assertEqual(self._run_git("cat-file", "blob", "HEAD:link.c").stdout, b"source.c")
        self.assertEqual(self._git("status", "--porcelain"), "")
        self._assert_rejected()

    def test_rejects_tracked_parent_directory_symlinked_outside_source(self):
        nested = self.source / "nested"
        nested.mkdir()
        (nested / "source.c").write_bytes(self.good)
        self._commit()
        self._verify()
        self._git("update-index", "--skip-worktree", "nested/source.c")
        outside = self.root / "outside-source-directory"
        nested.rename(outside)
        nested.symlink_to(outside, target_is_directory=True)
        (self.source / ".git/info/exclude").write_text("/nested\n")
        self.assertTrue(nested.is_symlink())
        self.assertEqual((nested / "source.c").read_bytes(), self.good)
        self.assertEqual(self._run_git("cat-file", "blob", "HEAD:nested/source.c").stdout,
                         self.good)
        self.assertEqual(self._git("status", "--porcelain"), "")
        self.assertEqual(self._git("rev-parse", "HEAD"), self.head)
        self._assert_rejected()

    def _external_metadata_symlink(self, relative):
        metadata = self.source / ".git" / relative
        outside = self.root / ("outside-" + relative)
        is_directory = metadata.is_dir()
        original_bytes = None if is_directory else metadata.read_bytes()
        metadata.rename(outside)
        metadata.symlink_to(outside, target_is_directory=is_directory)
        self.assertTrue(metadata.is_symlink())
        self.assertEqual(metadata.resolve(), outside.resolve())
        if original_bytes is not None:
            self.assertEqual(outside.read_bytes(), original_bytes)
        # Git itself may fail for external HEAD/refs; the guard must reject the
        # metadata link explicitly before depending on such version-specific errors.
        self._assert_rejected(metadata=True)

    def test_rejects_external_index_metadata_symlink(self):
        self._external_metadata_symlink("index")

    def test_rejects_external_head_metadata_symlink(self):
        self._external_metadata_symlink("HEAD")

    def test_rejects_external_config_metadata_symlink(self):
        self._external_metadata_symlink("config")

    def test_rejects_external_refs_metadata_symlink(self):
        self._external_metadata_symlink("refs")

    def test_rejects_corrupt_head_blob_when_connectivity_fsck_passes(self):
        blob = self._git("rev-parse", "HEAD:source.c")
        path = self.source / ".git/objects" / blob[:2] / blob[2:]
        self.assertTrue(path.is_file())
        self.assertEqual(self._run_git("cat-file", "blob", blob).stdout, self.good)
        self._git("fsck", "--full", "--no-dangling")
        path.chmod(0o600)
        path.write_bytes(zlib.compress(b"blob " + str(len(self.evil)).encode() + b"\0" + self.evil))
        self.assertEqual(self.filename.read_bytes(), self.good)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.head)
        self._git("fsck", "--connectivity-only", "--no-dangling")
        full = self._run_git("fsck", "--full", "--no-dangling", check=False)
        self.assertNotEqual(full.returncode, 0)
        self.assertIn(b"mismatch", full.stdout + full.stderr)
        self._assert_rejected()


class SourceObjectIntegrityTest(unittest.TestCase):
    """Git 返回的储存 OID 不能代替对象原始类型、长度和内容的独立校验。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name).resolve() / "source"
        self.source.mkdir()
        self._git("init", "-q", "-b", "master")
        self._git("config", "user.name", "对象原始字节 fixture")
        self._git("config", "user.email", "object@example.invalid")
        self.good = b"int good;\n"
        self.evil = b"int evil;\n"
        (self.source / "source.c").write_bytes(self.good)
        self._git("add", ".")
        self._git("commit", "-qm", "真实对象 fixture")
        self.head = self._git("rev-parse", "HEAD")
        self._verify()

    def _run_git(self, *args, check=True, input_bytes=None):
        return subprocess.run(["git", "-C", str(self.source), *args], check=check,
                              capture_output=True, input=input_bytes,
                              env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"})

    def _git(self, *args):
        return self._run_git(*args).stdout.decode().strip()

    def _verify(self):
        SDK.verify_complete_repository(self.source)

    def _corrupt_blob_preserving_storage_oid(self, blob):
        path = self.source / ".git/objects" / blob[:2] / blob[2:]
        self.assertTrue(path.is_file())
        path.chmod(0o600)
        path.write_bytes(zlib.compress(b"blob " + str(len(self.evil)).encode() + b"\0" + self.evil))
        self.assertEqual(self._git("rev-parse", "HEAD"), self.head)
        self.assertEqual((self.source / "source.c").read_bytes(), self.good)
        self.assertEqual(self._run_git("cat-file", "blob", blob).stdout, self.evil)
        batch = self._run_git("cat-file", "--batch", input_bytes=(blob + "\n").encode()).stdout
        self.assertEqual(batch, blob.encode() + b" blob " + str(len(self.evil)).encode()
                         + b"\n" + self.evil + b"\n")
        self.assertNotEqual(self._run_git("hash-object", "--stdin", input_bytes=self.evil)
                            .stdout.decode().strip(), blob)
        self._git("fsck", "--connectivity-only", "--no-dangling")
        with self.assertRaisesRegex(ValueError, "对象原始字节与 OID 不符"):
            self._verify()

    def test_rejects_head_blob_even_when_cat_file_reports_original_storage_oid(self):
        blob = self._git("rev-parse", "HEAD:source.c")
        self.assertEqual(self._run_git("cat-file", "blob", blob).stdout, self.good)
        self._corrupt_blob_preserving_storage_oid(blob)

    def test_rejects_corrupt_dangling_blob_when_connectivity_fsck_passes(self):
        payload = b"unreferenced original object;\n"
        blob = self._run_git("hash-object", "-w", "--stdin", input_bytes=payload).stdout.decode().strip()
        reachable = self._git("rev-list", "--all", "--objects").splitlines()
        self.assertFalse(any(line.split()[0] == blob for line in reachable))
        self.assertEqual(self._run_git("cat-file", "blob", blob).stdout, payload)
        self._verify()
        self._corrupt_blob_preserving_storage_oid(blob)


class SparseConfigurationTest(unittest.TestCase):
    """Git 最终生效的布尔配置与实际完整源码共同决定是否接受来源。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.global_config = self.root / "global.gitconfig"
        self.global_config.write_text("")
        self.included_config = self.root / "included.gitconfig"
        self.environment = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(self.global_config), "GIT_CONFIG_NOSYSTEM": "1",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.source = self.root / "source"
        self.source.mkdir()
        self._git("init", "-q", "-b", "master")
        self._git("config", "user.name", "稀疏配置 fixture")
        self._git("config", "user.email", "sparse@example.invalid")
        (self.source / "source.c").write_text("int source;\n")
        (self.source / "omitted").mkdir()
        (self.source / "omitted/source.c").write_text("int complete;\n")
        self._git("add", ".")
        self._git("commit", "-qm", "完整来源 fixture")

    def _git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.source), *args], check=check,
                              capture_output=True, text=True)

    def _set(self, key, value, *scope):
        self._git("config", *scope, key, value)

    def _verify(self):
        SDK.verify_complete_repository(self.source)

    def _effective_sparse(self):
        result = self._git("config", "--bool", "--get", "core.sparseCheckout", check=False)
        return result.returncode, result.stdout.strip()

    def _include(self, value):
        self.included_config.write_text("[core]\n\tsparseCheckout = " + value + "\n")
        self._set("include.path", str(self.included_config), "--local")

    def test_accepts_global_true_overridden_by_local_false(self):
        self._set("core.sparseCheckout", "true", "--global")
        self._set("core.sparseCheckout", "false", "--local")
        self.assertEqual(self._effective_sparse(), (0, "false"))
        self._verify()

    def test_rejects_global_false_overridden_by_local_true(self):
        self._set("core.sparseCheckout", "false", "--global")
        self._set("core.sparseCheckout", "true", "--local")
        self.assertEqual(self._effective_sparse(), (0, "true"))
        with self.assertRaisesRegex(ValueError, "sparse"):
            self._verify()

    def test_accepts_worktree_false_overriding_local_true(self):
        self._set("core.sparseCheckout", "true", "--local")
        self._set("extensions.worktreeConfig", "true", "--local")
        self._set("core.sparseCheckout", "false", "--worktree")
        self.assertEqual(self._effective_sparse(), (0, "false"))
        self._verify()

    def test_rejects_worktree_true_overriding_local_false(self):
        self._set("core.sparseCheckout", "false", "--local")
        self._set("extensions.worktreeConfig", "true", "--local")
        self._set("core.sparseCheckout", "true", "--worktree")
        self.assertEqual(self._effective_sparse(), (0, "true"))
        with self.assertRaisesRegex(ValueError, "sparse"):
            self._verify()

    def test_accepts_included_local_false_after_local_true(self):
        self._set("core.sparseCheckout", "true", "--local")
        self._include("false")
        self.assertEqual(self._effective_sparse(), (0, "false"))
        self._verify()

    def test_rejects_included_local_true_after_local_false(self):
        self._set("core.sparseCheckout", "false", "--local")
        self._include("true")
        self.assertEqual(self._effective_sparse(), (0, "true"))
        with self.assertRaisesRegex(ValueError, "sparse"):
            self._verify()

    def test_accepts_inactive_cone_mode_with_complete_source(self):
        self._set("core.sparseCheckout", "false", "--local")
        self._set("core.sparseCheckoutCone", "true", "--local")
        self.assertEqual(self._effective_sparse(), (0, "false"))
        self._verify()

    def test_rejects_invalid_effective_boolean(self):
        self._set("core.sparseCheckout", "invalid-boolean", "--local")
        self.assertNotEqual(self._effective_sparse()[0], 0)
        with self.assertRaises((ValueError, RuntimeError, subprocess.CalledProcessError)) as raised:
            self._verify()
        detail = getattr(raised.exception, "stderr", "") or str(raised.exception)
        self.assertIn("sparse", detail.lower())

    def test_rejects_nonzero_numeric_effective_true(self):
        self._set("core.sparseCheckout", "2", "--local")
        self.assertEqual(self._effective_sparse(), (0, "true"))
        with self.assertRaisesRegex(ValueError, "sparse"):
            self._verify()

    def test_rejects_implicit_boolean_effective_true(self):
        config = self.source / ".git/config"
        with config.open("a") as output:
            output.write("[core]\n\tsparseCheckout\n")
        self.assertEqual(self._effective_sparse(), (0, "true"))
        with self.assertRaisesRegex(ValueError, "sparse"):
            self._verify()


    def _effective_promisor(self, remote="origin"):
        result = self._git("config", "--bool", "--get", "remote." + remote + ".promisor", check=False)
        return result.returncode, result.stdout.strip()

    def test_accepts_explicit_promisor_false(self):
        self._set("remote.origin.promisor", "false", "--local")
        self.assertEqual(self._effective_promisor(), (0, "false"))
        self._verify()

    def test_accepts_local_promisor_false_overriding_global_true(self):
        self._set("remote.origin.promisor", "true", "--global")
        self._set("remote.origin.promisor", "false", "--local")
        self.assertEqual(self._effective_promisor(), (0, "false"))
        self._verify()

    def test_accepts_worktree_promisor_false_overriding_local_true(self):
        self._set("remote.origin.promisor", "true", "--local")
        self._set("extensions.worktreeConfig", "true", "--local")
        self._set("remote.origin.promisor", "false", "--worktree")
        self.assertEqual(self._effective_promisor(), (0, "false"))
        self._verify()

    def test_accepts_included_promisor_false_overriding_local_true(self):
        self._set("remote.origin.promisor", "true", "--local")
        self.included_config.write_text('[remote "origin"]\n\tpromisor = false\n')
        self._set("include.path", str(self.included_config), "--local")
        self.assertEqual(self._effective_promisor(), (0, "false"))
        self._verify()

    def test_accepts_false_mixed_case_remote_subsection(self):
        self._set("remote.Mirror.Name.promisor", "false", "--local")
        self.assertEqual(self._effective_promisor("Mirror.Name"), (0, "false"))
        self._verify()

    def test_rejects_true_mixed_case_remote_despite_lowercase_false(self):
        self._set("remote.Mirror.Name.promisor", "true", "--local")
        self._set("remote.mirror.name.promisor", "false", "--local")
        self.assertEqual(self._effective_promisor("Mirror.Name"), (0, "true"))
        self.assertEqual(self._effective_promisor("mirror.name"), (0, "false"))
        with self.assertRaisesRegex(ValueError, "partial"):
            self._verify()

    def test_rejects_local_promisor_true_overriding_global_false(self):
        self._set("remote.origin.promisor", "false", "--global")
        self._set("remote.origin.promisor", "true", "--local")
        self.assertEqual(self._effective_promisor(), (0, "true"))
        with self.assertRaisesRegex(ValueError, "partial"):
            self._verify()

    def test_rejects_nonzero_numeric_promisor_true(self):
        self._set("remote.origin.promisor", "2", "--local")
        self.assertEqual(self._effective_promisor(), (0, "true"))
        with self.assertRaisesRegex(ValueError, "partial"):
            self._verify()

    def test_rejects_implicit_promisor_true(self):
        with (self.source / ".git/config").open("a") as output:
            output.write('[remote "origin"]\n\tpromisor\n')
        self.assertEqual(self._effective_promisor(), (0, "true"))
        with self.assertRaisesRegex(ValueError, "partial"):
            self._verify()

    def test_rejects_invalid_effective_promisor_boolean(self):
        self._set("remote.origin.promisor", "invalid-boolean", "--local")
        self.assertNotEqual(self._effective_promisor()[0], 0)
        with self.assertRaises((ValueError, RuntimeError, subprocess.CalledProcessError)) as raised:
            self._verify()
        detail = getattr(raised.exception, "stderr", "") or str(raised.exception)
        self.assertIn("promisor", detail.lower())

    def test_rejects_partial_filter_marker_with_promisor_false(self):
        self._set("remote.origin.promisor", "false", "--local")
        self._set("remote.origin.partialCloneFilter", "blob:none", "--local")
        with self.assertRaisesRegex(ValueError, "partial"):
            self._verify()

    def _assert_ignored_rejected(self):
        self.assertEqual(self._git("ls-files", "--others", "--exclude-standard").stdout, "")
        self.assertTrue(self._git("ls-files", "--others", "-z").stdout)
        with self.assertRaisesRegex(ValueError, "ignored|untracked"):
            self._verify()

    def test_rejects_source_hidden_by_git_info_exclude(self):
        with (self.source / ".git/info/exclude").open("a") as output:
            output.write("injected.c\n")
        (self.source / "injected.c").write_text("int injected;\n")
        self._assert_ignored_rejected()

    def test_rejects_source_hidden_by_tracked_gitignore(self):
        (self.source / ".gitignore").write_text("injected.c\n")
        self._git("add", ".gitignore")
        self._git("commit", "-qm", "显式源码忽略规则 fixture")
        (self.source / "injected.c").write_text("int injected;\n")
        self._assert_ignored_rejected()

    def test_rejects_source_hidden_by_global_ignore(self):
        ignore_file = self.root / "global.ignore"
        ignore_file.write_text("injected.c\n")
        self._set("core.excludesFile", str(ignore_file), "--global")
        (self.source / "injected.c").write_text("int injected;\n")
        self._assert_ignored_rejected()

    def test_rejects_ignored_source_inside_cache_named_directory(self):
        with (self.source / ".git/info/exclude").open("a") as output:
            output.write("__pycache__/\n")
        cache = self.source / "__pycache__"
        cache.mkdir()
        (cache / "injected.py").write_text("runtime_value = 37\n")
        self._assert_ignored_rejected()

    def test_rejects_ignored_source_symlink(self):
        outside = self.root / "outside.c"
        outside.write_text("int injected;\n")
        with (self.source / ".git/info/exclude").open("a") as output:
            output.write("injected.c\n")
        (self.source / "injected.c").symlink_to(outside)
        self._assert_ignored_rejected()


    def _omit_tracked_source(self):
        self._git("sparse-checkout", "set", "--no-cone", "/source.c")
        self.assertTrue((self.source / "source.c").is_file())
        self.assertFalse((self.source / "omitted/source.c").exists())

    def test_rejects_actual_sparse_checkout(self):
        self._omit_tracked_source()
        self.assertEqual(self._effective_sparse(), (0, "true"))
        with self.assertRaisesRegex(ValueError, "sparse"):
            self._verify()

    def test_rejects_missing_raw_source_after_sparse_flag_disabled(self):
        self._omit_tracked_source()
        self._set("core.sparseCheckout", "false", "--worktree")
        self.assertEqual(self._effective_sparse(), (0, "false"))
        with self.assertRaises((ValueError, FileNotFoundError)):
            self._verify()


class SDKMainRecursiveSourceTest(unittest.TestCase):
    """实际 SDK CLI 必须核对锁定递归树，同时允许唯一 lwIP 覆盖。"""

    run_git = SDKContractTest.run_git
    commit = SDKContractTest.commit

    def setUp(self):
        SDKContractTest.setUp(self)
        SDKContractTest.pristine(self)
        leaf_upstream = self.root / "leaf-upstream"
        framework_upstream = self.root / "framework-upstream"
        self.good = b"int dependency_good;\n"
        self.evil = b"int dependency_evil;\n"
        for repository in (leaf_upstream, framework_upstream):
            repository.mkdir()
            self._git(repository, "init", "-q", "-b", "master")
            self._git(repository, "config", "user.name", "递归 SDK fixture")
            self._git(repository, "config", "user.email", "sdk-graph@example.invalid")
        (leaf_upstream / "source.c").write_bytes(self.good)
        self.leaf_original = self._commit(leaf_upstream)
        self._git(framework_upstream, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                  str(leaf_upstream), "leaf")
        self.framework_original = self._commit(framework_upstream)
        self._git(self.sdk, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                  str(framework_upstream), "framework")
        self.lwip_original = self.original_lwip
        self.lwip_fixed = self.lock["lwip"]["revision"]
        self._git(self.lwip, "checkout", "-q", "--detach", self.lwip_original)
        self.sdk_original = self._commit(self.sdk)
        self._git(self.sdk, "-c", "protocol.file.allow=always", "submodule", "update", "--init",
                  "--recursive", "--checkout")
        self._git(self.lwip, "checkout", "-q", "--detach", self.lwip_fixed)
        self.framework = self.sdk / "framework"
        self.leaf = self.framework / "leaf"
        self.lock["idf"]["revision"] = self.sdk_original
        self.recipe["idf"]["revision"] = self.sdk_original
        self.recipe_bytes = encoded(self.recipe)
        self.lock["sdk_derivation"]["sha256"] = SDK.digest(self.recipe_bytes)
        (self.sdk / "sdk.c").write_text("official idf plus approved capacity statistics\n")
        (self.tlsf / "tlsf.c").write_text("official tlsf plus approved capacity statistics\n")
        self.stamp.write_bytes(self.recipe_bytes)
        self.stamp.chmod(0o400)

    def _run_git(self, repository, *args):
        return subprocess.run(["git", "-C", str(repository), *args], check=True,
                              capture_output=True, env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"})

    def _git(self, repository, *args):
        return self._run_git(repository, *args).stdout.decode().strip()

    def _commit(self, repository):
        self._git(repository, "add", ".")
        self._git(repository, "commit", "-qm", "完整 SDK fixture")
        return self._git(repository, "rev-parse", "HEAD")

    def _assert_main(self, accepted, error=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        injection = patch.object(SDK, "read_lock", return_value=self.lock)
        argv = ["source-fixture", "check", "--path", str(self.sdk), "--quiet"]
        with injection, patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            try:
                result = SDK.main()
            except SystemExit as failure:
                result = failure.code
        self.assertEqual(result == 0, accepted, stdout.getvalue() + stderr.getvalue())
        if error:
            self.assertIn(error, stderr.getvalue())

    def _hide_child_status(self):
        self._git(self.sdk, "config", "submodule.framework.ignore", "all")
        self._git(self.framework, "config", "submodule.leaf.ignore", "all")
        self._git(self.sdk, "update-index", "--skip-worktree", "framework")
        self._git(self.leaf, "update-index", "--skip-worktree", "source.c")

    def test_main_accepts_complete_absorbed_graph_with_only_lwip_override(self):
        self.assertEqual(self._git(self.sdk, "rev-parse", "HEAD"), self.sdk_original)
        self.assertEqual(self._git(self.sdk, "rev-parse", "HEAD:components/lwip/lwip"), self.lwip_original)
        self.assertEqual(self._git(self.lwip, "rev-parse", "HEAD"), self.lwip_fixed)
        self.assertEqual(self._git(self.sdk, "status", "--porcelain", "--ignore-submodules=none"),
                         "M components/heap/tlsf\n M components/lwip/lwip\n M sdk.c\n?? " + SDK.DERIVATION_STAMP)
        self._assert_main(True)
        self._hide_child_status()
        self._assert_main(True)

    def test_main_rejects_hidden_child_raw_bytes_with_unchanged_index(self):
        self._hide_child_status()
        (self.leaf / "source.c").write_bytes(self.evil)
        self.assertEqual(self._git(self.leaf, "status", "--porcelain"), "")
        self.assertEqual(self._git(self.framework, "status", "--porcelain"), "")
        self.assertEqual(self._git(self.sdk, "rev-parse", "HEAD"), self.sdk_original)
        self.assertEqual(self._run_git(self.leaf, "cat-file", "blob", "HEAD:source.c").stdout, self.good)
        self.assertEqual((self.leaf / "source.c").read_bytes(), self.evil)
        self.assertEqual(self._git(self.sdk, "ls-files", "--stage", "--", "framework"),
                         "160000 " + self.framework_original + " 0\tframework")
        self.assertEqual(self._git(self.leaf, "ls-files", "--stage", "--", "source.c").split()[1],
                         self._git(self.leaf, "rev-parse", "HEAD:source.c"))
        self._assert_main(False, "原始字节")

    def test_main_rejects_hidden_staged_gitlink_removal_with_all_source_bytes_unchanged(self):
        self._hide_child_status()
        self._git(self.sdk, "update-index", "--no-skip-worktree", "framework")
        self._git(self.sdk, "update-index", "--force-remove", "framework")
        (self.sdk / ".git/info/exclude").write_text("/framework\n")
        self.assertEqual(self._git(self.sdk, "diff", "--cached", "--name-only"), "")
        self.assertNotIn(" framework", self._git(self.sdk, "submodule", "status", "--recursive"))
        self.assertEqual(self._git(self.sdk, "ls-files", "--stage", "--", "framework"), "")
        self.assertTrue(self._git(self.sdk, "ls-tree", self.sdk_original, "--", "framework")
                        .startswith("160000 "))
        self.assertEqual(self._run_git(self.leaf, "cat-file", "blob", "HEAD:source.c").stdout, self.good)
        self.assertEqual((self.leaf / "source.c").read_bytes(), self.good)
        self.assertEqual(self._git(self.leaf, "rev-parse", "HEAD"), self.leaf_original)
        self._assert_main(False, "索引")


if __name__ == "__main__":
    unittest.main()
