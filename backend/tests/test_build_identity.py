from cosmos_quant.build_identity import capture_runtime_build_identity


def test_runtime_build_identity_binds_git_tree_and_detects_dirty_workspace():
    identity = capture_runtime_build_identity()

    assert len(identity.git_commit_sha) == 40
    assert len(identity.source_tree_hash) == 64
    # The test suite is normally executed while upgrade files are uncommitted.
    assert isinstance(identity.working_tree_dirty, bool)
    assert identity.formal_certificate_eligible is (not identity.working_tree_dirty)


def test_unicode_git_paths_and_significant_spaces(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    path = tmp_path / " 中文 文件.txt"
    path.write_text("first", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "fixture")
    before = capture_runtime_build_identity(tmp_path)
    assert not before.working_tree_dirty
    path.write_text("second", encoding="utf-8")
    after = capture_runtime_build_identity(tmp_path)
    assert before.source_tree_hash != after.source_tree_hash
    assert after.working_tree_dirty
