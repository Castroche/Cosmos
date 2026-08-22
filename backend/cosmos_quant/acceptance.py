from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class AcceptanceCheck(BaseModel):
    code: str
    label: str
    weight: int = Field(ge=0, le=100)
    passed: bool
    evidence: str


class AcceptanceDimension(BaseModel):
    code: str
    label: str
    score: int = Field(ge=0, le=100)
    hard_gate_passed: bool = True
    checks: list[AcceptanceCheck]

    @model_validator(mode="after")
    def weights_total_one_hundred(self):
        if sum(check.weight for check in self.checks) != 100:
            raise ValueError(f"{self.code} acceptance weights must total 100")
        expected = sum(check.weight for check in self.checks if check.passed)
        if not self.hard_gate_passed:
            expected = 0
        if self.score != expected:
            raise ValueError(f"{self.code} score does not match verified checks")
        return self


class ProductAcceptanceReport(BaseModel):
    schema_version: Literal[1] = 1
    scope: str = "cosmos-core-v1"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    overall_score: Decimal
    engineering_score: Decimal
    capability_score: Decimal = Decimal("0")
    strategy_certified: bool = False
    strategy_certification_status: Literal["certified", "hold"] = "hold"
    profitability_evidence_score: int
    profitability_guaranteed: Literal[False] = False
    dimensions: list[AcceptanceDimension]
    blockers: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


def _check(
    code: str,
    label: str,
    weight: int,
    passed: bool,
    evidence: Any,
) -> AcceptanceCheck:
    return AcceptanceCheck(
        code=code,
        label=label,
        weight=weight,
        passed=bool(passed),
        evidence=str(evidence),
    )


def _dimension(
    code: str,
    label: str,
    checks: list[AcceptanceCheck],
    *,
    hard_gate_passed: bool = True,
) -> AcceptanceDimension:
    score = sum(check.weight for check in checks if check.passed) if hard_gate_passed else 0
    return AcceptanceDimension(
        code=code,
        label=label,
        score=score,
        hard_gate_passed=hard_gate_passed,
        checks=checks,
    )


def build_product_acceptance_report(evidence: dict[str, Any]) -> ProductAcceptanceReport:
    infrastructure = _dimension(
        "research_infrastructure",
        "量化研究终端基础设施",
        [
            _check(
                "full_regression",
                "完整后端与前端回归",
                20,
                evidence.get("full_regression_passed"),
                evidence.get("full_regression_summary", "未运行"),
            ),
            _check(
                "durable_campaign",
                "持久化 Campaign 完成",
                15,
                evidence.get("latest_campaign_completed"),
                evidence.get("latest_campaign_summary", "无"),
            ),
            _check(
                "campaign_cache",
                "单元级缓存可重放",
                15,
                evidence.get("campaign_cache_complete"),
                evidence.get("campaign_cache_summary", "无"),
            ),
            _check(
                "immutable_datasets",
                "Campaign 数据集指纹全部验证",
                20,
                evidence.get("campaign_datasets_verified"),
                evidence.get("campaign_dataset_summary", "无"),
            ),
            _check(
                "process_isolation",
                "CPU 回测进程隔离",
                15,
                evidence.get("research_process_isolated"),
                evidence.get("research_process_summary", "未验证"),
            ),
            _check(
                "no_stuck_jobs",
                "无陈旧运行任务",
                15,
                evidence.get("no_stuck_research_jobs"),
                evidence.get("research_job_summary", "未知"),
            ),
        ],
    )
    agent = _dimension(
        "agent_research_loop",
        "Agent 自主研究闭环",
        [
            _check(
                "market_sensing",
                "完整概率化市场感知",
                20,
                evidence.get("market_sensing_complete"),
                evidence.get("market_sensing_summary", "无"),
            ),
            _check(
                "library_retrieval",
                "策略库检索",
                10,
                evidence.get("strategy_library_present"),
                evidence.get("strategy_library_summary", "无"),
            ),
            _check(
                "agent_generation",
                "Agent 候选生成",
                10,
                evidence.get("agent_candidates_present"),
                evidence.get("agent_candidate_summary", "无"),
            ),
            _check(
                "failure_revision",
                "失败驱动父子版本修改",
                10,
                evidence.get("failure_revision_present"),
                evidence.get("failure_revision_summary", "无"),
            ),
            _check(
                "independent_evaluator",
                "独立反过拟合验证器",
                10,
                evidence.get("independent_evaluator_present"),
                evidence.get("independent_evaluator_summary", "无"),
            ),
            _check(
                "research_budget",
                "有界搜索与重复试验约束",
                5,
                evidence.get("research_budget_enforced"),
                evidence.get("research_budget_summary", "无"),
            ),
            _check(
                "negative_memory",
                "失败与退役策略记忆",
                10,
                evidence.get("negative_strategy_memory"),
                evidence.get("negative_strategy_memory_summary", "无"),
            ),
            _check(
                "revision_effectiveness",
                "修订产生稳健改进",
                10,
                evidence.get("revision_effective"),
                evidence.get("revision_effective_summary", "无"),
            ),
            _check(
                "agent_family_coverage",
                "研究覆盖至少25个策略家族",
                10,
                evidence.get("strategy_family_target_met"),
                evidence.get("strategy_family_summary", "无"),
            ),
            _check(
                "honest_abstention",
                "无合格策略时保持空仓",
                5,
                evidence.get("honest_abstention"),
                evidence.get("abstention_summary", "未知"),
            ),
        ],
    )
    data = _dimension(
        "crypto_data_completeness",
        "加密市场数据完整度",
        [
            _check(
                "core_universe",
                "核心币种与周期覆盖",
                15,
                evidence.get("core_universe_complete"),
                evidence.get("core_universe_summary", "无"),
            ),
            _check(
                "point_in_time_universe",
                "点时市场宇宙与历史覆盖契约完整物化",
                15,
                evidence.get("point_in_time_universe_complete"),
                evidence.get("point_in_time_universe_summary", "未验证"),
            ),
            _check(
                "survivorship_bias",
                "生存偏差由历史市场档案或前瞻样本控制",
                15,
                evidence.get("survivorship_bias_controlled"),
                evidence.get("survivorship_bias_summary", "未验证"),
            ),
            _check(
                "candle_quality",
                "K线无缺口且无非法值",
                15,
                evidence.get("core_candles_verified"),
                evidence.get("candle_quality_summary", "无"),
            ),
            _check(
                "funding_history",
                "历史资金费率覆盖",
                10,
                evidence.get("funding_coverage_complete"),
                evidence.get("funding_coverage_summary", "无"),
            ),
            _check(
                "open_interest_history",
                "历史持仓量覆盖",
                10,
                evidence.get("open_interest_coverage_complete"),
                evidence.get("open_interest_coverage_summary", "无"),
            ),
            _check(
                "multi_exchange",
                "至少两个交易所独立数据",
                10,
                evidence.get("multi_exchange_data"),
                evidence.get("multi_exchange_summary", "无"),
            ),
            _check(
                "crypto_native_depth",
                "基差、爆仓和订单流历史",
                10,
                evidence.get("crypto_native_depth_complete"),
                evidence.get("crypto_native_depth_summary", "无"),
            ),
        ],
    )
    strategy = _dimension(
        "reliable_strategy_generation",
        "自动产生可靠策略的能力",
        [
            _check(
                "family_coverage",
                "至少25个不同策略家族",
                20,
                evidence.get("strategy_family_target_met"),
                evidence.get("strategy_family_summary", "无"),
            ),
            _check(
                "template_coverage",
                "至少80个标准策略模板",
                15,
                evidence.get("strategy_template_target_met"),
                evidence.get("strategy_template_summary", "无"),
            ),
            _check(
                "campaign_matrix",
                "跨市场矩阵完整执行",
                15,
                evidence.get("campaign_matrix_complete"),
                evidence.get("latest_campaign_summary", "无"),
            ),
            _check(
                "pbo",
                "PBO置信上界通过",
                15,
                evidence.get("latest_campaign_pbo_passed"),
                evidence.get("pbo_summary", "无"),
            ),
            _check(
                "robust_champion",
                "存在跨市场稳健 Champion",
                20,
                evidence.get("robust_champion_present"),
                evidence.get("champion_summary", "无"),
            ),
            _check(
                "strategy_certificate",
                "不可篡改策略执行证书",
                15,
                evidence.get("strategy_certificate_enforced"),
                evidence.get("strategy_certificate_summary", "未实现"),
            ),
        ],
    )
    live = _dimension(
        "autonomous_live_readiness",
        "自主实盘准备度",
        [
            _check(
                "reconciliation",
                "订单与仓位无不确定状态",
                15,
                evidence.get("execution_reconciled"),
                evidence.get("reconciliation_summary", "未知"),
            ),
            _check(
                "idempotency",
                "订单幂等账本",
                15,
                evidence.get("idempotent_order_ledger"),
                evidence.get("idempotency_summary", "未验证"),
            ),
            _check(
                "native_protection",
                "成交后原生保护单",
                15,
                evidence.get("native_protection_verified"),
                evidence.get("native_protection_summary", "未验证"),
            ),
            _check(
                "fault_injection",
                "执行故障注入套件",
                20,
                evidence.get("fault_injection_passed"),
                evidence.get("fault_injection_summary", "未运行"),
            ),
            _check(
                "restart_recovery",
                "重启恢复验证",
                15,
                evidence.get("restart_recovery_passed"),
                evidence.get("restart_recovery_summary", "未运行"),
            ),
            _check(
                "certificate_enforcement",
                "只有认证策略可以开仓",
                20,
                evidence.get("strategy_certificate_enforced"),
                evidence.get("strategy_certificate_summary", "未实现"),
            ),
        ],
    )
    profitability_gate = bool(evidence.get("robust_champion_present"))
    profitability = _dimension(
        "profitability_evidence",
        "稳定盈利证据等级",
        [
            _check(
                "anti_overfit",
                "PBO与多重检验",
                15,
                evidence.get("latest_campaign_pbo_passed"),
                evidence.get("pbo_summary", "无"),
            ),
            _check(
                "oos_robustness",
                "跨市场样本外稳健",
                20,
                evidence.get("robust_champion_present"),
                evidence.get("champion_summary", "无"),
            ),
            _check(
                "cost_stress",
                "成本压力后仍盈利",
                15,
                evidence.get("champion_cost_stress_passed"),
                evidence.get("champion_cost_summary", "无Champion"),
            ),
            _check(
                "parameter_stability",
                "参数邻域稳定",
                10,
                evidence.get("champion_parameter_stable"),
                evidence.get("champion_parameter_summary", "无Champion"),
            ),
            _check(
                "cross_market",
                "收益不依赖单一币种",
                10,
                evidence.get("champion_cross_market"),
                evidence.get("champion_cross_market_summary", "无Champion"),
            ),
            _check(
                "win_payoff",
                "胜率与盈亏比置信下界达标",
                10,
                evidence.get("champion_win_payoff_passed"),
                evidence.get("champion_win_payoff_summary", "无Champion"),
            ),
            _check(
                "portfolio",
                "低相关组合通过",
                10,
                evidence.get("qualified_portfolio_present"),
                evidence.get("portfolio_summary", "无"),
            ),
            _check(
                "causal_execution",
                "事件驱动真实成交证据",
                10,
                evidence.get("causal_live_evidence_passed"),
                evidence.get("causal_live_summary", "无"),
            ),
        ],
        hard_gate_passed=profitability_gate,
    )
    dimensions = [infrastructure, agent, data, strategy, live, profitability]
    engineering = dimensions[:-1]
    engineering_score = Decimal(sum(item.score for item in engineering)) / Decimal(
        len(engineering)
    )
    overall_score = Decimal(sum(item.score for item in dimensions)) / Decimal(len(dimensions))

    # Capability deliberately excludes the outcome checks that say whether the
    # current search happened to discover a profitable strategy.  This keeps a
    # complete research/execution system from being confused with a certified
    # strategy, while still charging capability for missing historical data.
    capability_check_codes = {
        "research_infrastructure": {check.code for check in infrastructure.checks},
        "agent_research_loop": {
            check.code for check in agent.checks if check.code != "revision_effectiveness"
        },
        "crypto_data_completeness": {check.code for check in data.checks},
        "reliable_strategy_generation": {
            "family_coverage",
            "template_coverage",
            "campaign_matrix",
            "strategy_certificate",
        },
        "autonomous_live_readiness": {check.code for check in live.checks},
    }

    def normalized_capability_score(dimension: AcceptanceDimension) -> Decimal:
        included = [
            check
            for check in dimension.checks
            if check.code in capability_check_codes[dimension.code]
        ]
        possible = sum(check.weight for check in included)
        achieved = sum(check.weight for check in included if check.passed)
        return Decimal(achieved) * Decimal("100") / Decimal(possible)

    capability_dimensions = [infrastructure, agent, data, strategy, live]
    capability_score = sum(
        (normalized_capability_score(item) for item in capability_dimensions),
        Decimal("0"),
    ) / Decimal(len(capability_dimensions))
    strategy_certified = bool(
        evidence.get("robust_champion_present")
        and evidence.get("latest_campaign_pbo_passed")
        and evidence.get("strategy_certificate_enforced")
    )
    blockers = [
        check.label
        for dimension in dimensions
        for check in dimension.checks
        if not check.passed
    ]
    if not profitability_gate:
        blockers.insert(0, "没有跨市场稳健 Champion，盈利证据分强制为0")
    return ProductAcceptanceReport(
        overall_score=overall_score.quantize(Decimal("0.1")),
        engineering_score=engineering_score.quantize(Decimal("0.1")),
        capability_score=capability_score.quantize(Decimal("0.1")),
        strategy_certified=strategy_certified,
        strategy_certification_status="certified" if strategy_certified else "hold",
        profitability_evidence_score=profitability.score,
        dimensions=dimensions,
        blockers=list(dict.fromkeys(blockers)),
        evidence=evidence,
    )
