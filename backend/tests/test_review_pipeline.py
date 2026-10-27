from cosmos_quant.review_pipeline import (
    StrategyReviewJob,
    StrategyReviewJobStatus,
    review_failure_detail,
    should_retry_review_after_restart,
    strategy_review_fingerprint,
)
from cosmos_quant.storage import SqliteStore


def test_strategy_review_job_persists_and_is_idempotent(tmp_path):
    store = SqliteStore(tmp_path / "cosmos.db")
    fingerprint = strategy_review_fingerprint("spec-123", entry_profile="balanced_5m_v1")
    job = StrategyReviewJob(
        candidate_id="cand_test",
        candidate_fingerprint="spec-123",
        review_fingerprint=fingerprint,
    )
    store.save_strategy_review_job(job)

    loaded = StrategyReviewJob.model_validate(store.load_strategy_review_job(job.id))
    assert loaded.status == StrategyReviewJobStatus.QUEUED
    assert store.load_strategy_review_job_by_fingerprint(fingerprint)["id"] == job.id

    loaded.status = StrategyReviewJobStatus.RUNNING
    loaded.progress_percent = 35
    store.save_strategy_review_job(loaded)
    rows = store.list_strategy_review_jobs()
    assert len(rows) == 1
    assert rows[0]["progress_percent"] == 35


def test_review_failure_detail_never_loses_empty_exception_type():
    detail = review_failure_detail(AssertionError())

    assert detail == "AssertionError: exception did not provide a message"


def test_v2_review_fingerprint_is_idempotent_but_scope_and_event_specific():
    common = {
        "candidate_fingerprint": "spec-123",
        "entry_profile": "balanced_5m_v1",
        "timeframe": "5m",
        "policy_fingerprint": "policy-v2",
        "review_contract_version": 2,
    }

    btc = strategy_review_fingerprint(
        candidate_id="cand_event_a",
        symbol="BTC/USDT",
        **common,
    )

    assert btc == strategy_review_fingerprint(
        candidate_id="cand_event_a",
        symbol="BTC/USDT",
        **common,
    )
    assert btc != strategy_review_fingerprint(
        candidate_id="cand_event_a",
        symbol="ETH/USDT",
        **common,
    )
    assert btc != strategy_review_fingerprint(
        candidate_id="cand_event_b",
        symbol="BTC/USDT",
        **common,
    )


def test_legacy_review_payload_remains_readable_as_v1():
    legacy = StrategyReviewJob.model_validate(
        {
            "candidate_id": "cand_legacy",
            "candidate_fingerprint": "spec-old",
            "review_fingerprint": "review-old",
        }
    )

    assert legacy.review_contract_version == 1
    assert legacy.scope_results == []


def test_restart_retries_legacy_opaque_block_but_respects_attempt_cap():
    job = StrategyReviewJob(
        candidate_id="cand_test",
        candidate_fingerprint="spec-123",
        review_fingerprint="review-123",
        status=StrategyReviewJobStatus.BLOCKED,
        stage="infrastructure_blocked",
        error="",
        attempt_count=1,
    )

    assert should_retry_review_after_restart(job) is True
    assert should_retry_review_after_restart(job.model_copy(update={"attempt_count": 3})) is False
    assert should_retry_review_after_restart(
        job.model_copy(update={"error": "ValueError: invalid candidate"})
    ) is False
