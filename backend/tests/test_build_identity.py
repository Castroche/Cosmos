from cosmos_quant.build_identity import capture_runtime_build_identity


def test_runtime_build_identity_binds_git_tree_and_detects_dirty_workspace():
    identity = capture_runtime_build_identity()

    assert len(identity.git_commit_sha) == 40
    assert len(identity.source_tree_hash) == 64
    # The test suite is normally executed while upgrade files are uncommitted.
    assert isinstance(identity.working_tree_dirty, bool)
    assert identity.formal_certificate_eligible is (not identity.working_tree_dirty)
