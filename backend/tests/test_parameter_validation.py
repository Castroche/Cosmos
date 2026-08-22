from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.agent.parameter_validation import (
    ParameterFoldEvidence,
    build_rolling_parameter_report,
)
from cosmos_quant.backtest import BacktestWindowSummary


def _window(
    index: int,
    *,
    return_percent: str,
    profit_factor: str,
    trades: int = 5,
) -> BacktestWindowSummary:
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index * 10)
    return BacktestWindowSummary(
        label=f"chronological_{index + 1}",
        sample_bars=100,
        data_start=start,
        data_end=start + timedelta(days=9),
        net_profit=Decimal(return_percent) * Decimal("10"),
        return_percent=Decimal(return_percent),
        max_drawdown_percent=Decimal("0.5"),
        win_rate_percent=Decimal("52"),
        profit_factor=Decimal(profit_factor),
        total_trades=trades,
    )


def test_parameter_walk_forward_uses_only_prior_folds_for_selection():
    responsive = ParameterFoldEvidence(
        label="responsive",
        spec_fingerprint="responsive-spec",
        windows=[
            _window(0, return_percent="2", profit_factor="1.5"),
            _window(1, return_percent="1", profit_factor="1.3"),
            _window(2, return_percent="-0.5", profit_factor="0.8"),
            _window(3, return_percent="1", profit_factor="1.2"),
        ],
    )
    selective = ParameterFoldEvidence(
        label="selective",
        spec_fingerprint="selective-spec",
        windows=[
            _window(0, return_percent="0.5", profit_factor="1.1"),
            _window(1, return_percent="2", profit_factor="1.6"),
            _window(2, return_percent="1", profit_factor="1.3"),
            _window(3, return_percent="1", profit_factor="1.2"),
        ],
    )

    report = build_rolling_parameter_report([responsive, selective])

    assert report.lookahead_safe is True
    assert report.selection_basis == "prior_folds_only"
    assert report.execution_gate is False
    assert report.steps[0].selected_label == "responsive"
    assert report.steps[0].validation_fold == "chronological_2"
    assert report.evaluable_steps == 3
    assert report.positive_steps == 2
    assert report.passed is True


def test_parameter_walk_forward_reports_sparse_validation_without_hard_gate_claim():
    sparse = [
        ParameterFoldEvidence(
            label=label,
            spec_fingerprint=f"{label}-spec",
            windows=[
                _window(index, return_percent="1", profit_factor="1.2", trades=0)
                for index in range(4)
            ],
        )
        for label in ("baseline", "responsive")
    ]

    report = build_rolling_parameter_report(sparse)

    assert report.passed is False
    assert report.execution_gate is False
    assert report.evaluable_steps == 0
    assert any("最低样本" in reason for reason in report.reasons)


def test_single_preselected_spec_uses_strict_fixed_forward_validation():
    fixed = ParameterFoldEvidence(
        label="responsive",
        spec_fingerprint="responsive-spec",
        windows=[
            _window(0, return_percent="0.2", profit_factor="1.1", trades=4),
            _window(1, return_percent="0.4", profit_factor="1.2", trades=5),
            _window(2, return_percent="0.7", profit_factor="1.4", trades=8),
        ],
    )

    report = build_rolling_parameter_report([fixed])

    assert report.mode == "fixed_spec_forward_validation"
    assert report.selection_basis == "current_market_fixed_spec"
    assert report.candidate_count == 1
    assert report.evaluable_steps == 2
    assert report.positive_step_ratio == Decimal("1")
    assert report.passed is True
    assert "唯一规格" in report.reasons[0]


def test_single_preselected_spec_fails_when_any_forward_fold_is_negative():
    fixed = ParameterFoldEvidence(
        label="responsive",
        spec_fingerprint="responsive-spec",
        windows=[
            _window(0, return_percent="0.2", profit_factor="1.1", trades=4),
            _window(1, return_percent="-0.1", profit_factor="0.9", trades=5),
            _window(2, return_percent="0.7", profit_factor="1.4", trades=8),
        ],
    )

    report = build_rolling_parameter_report([fixed])

    assert report.evaluable_steps == 2
    assert report.positive_step_ratio == Decimal("0.5")
    assert report.passed is False
    assert any("低于 100%" in reason for reason in report.reasons)
