from __future__ import annotations

import asyncio
import json

from fastapi import HTTPException

from .ai_configuration import AIProvider
from .ai_gateway import AIErrorCode, AIProviderError
from .api_contracts import AIProposalItem, AIProposalRequest, AIProposalResponse
from .entry_profiles import DEFAULT_ENTRY_PROFILE_ID
from .operator_workspace import stable_fingerprint
from .research_lifecycle import CandidateResearchContext, CandidateStatus, create_candidate
from .strategy_spec import strategy_spec_json_schema, validate_strategy_spec

logger = __import__("logging").getLogger(__name__)


class ResearchProposalsService:
    async def propose_ai_strategy_candidates(self, request: AIProposalRequest):
        configuration = self.configuration_service.current_configuration()
        symbol = request.symbol or configuration.default_symbol
        timeframe = request.timeframe or configuration.default_timeframe
        try:
            market, history = await self.runtime.market_context(
                limit=2000,
                symbol=symbol,
                timeframe=timeframe,
            )
            summary = await self.build_ai_generation_summary(
                symbol=symbol,
                timeframe=timeframe,
                origin_market=market,
                origin_history=history,
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

        failure_digest, rejected_fingerprints = await asyncio.to_thread(
            self.recent_candidate_failure_digest,
            symbol=symbol,
            timeframe=timeframe,
            family=request.family,
        )

        self.ai_gateway.service = self.ai_configuration_service
        self.ai_gateway.proxy_url = configuration.network_proxy_url
        ai_view = self.ai_configuration_service.get()
        system_prompt = (
            "You are a cost-aware quantitative research assistant. Propose exactly one "
            "low-turnover parameterized strategy candidate. Never propose order execution, "
            "leverage changes, credentials, or guaranteed returns. Use only the supplied causal "
            "closed-bar summaries and failure digest. Prefer parameters whose ATR stop distance "
            "can absorb the supplied round-trip costs. Do not repeat a rejected fingerprint. "
            "The output is only a hypothesis and must pass deterministic validation."
        )
        requested_family = (
            request.family.value if request.family else "select the best supported family"
        )
        user_prompt = (
            f"Operator hypothesis: {request.hypothesis}\n"
            f"Requested family: {requested_family}\n"
            f"Causal multi-timeframe summary: "
            f"{json.dumps(summary, ensure_ascii=False, sort_keys=True)}\n"
            "Execution economics: "
            f"fee_bps={configuration.fee_bps}, slippage_bps={configuration.slippage_bps}, "
            f"risk_fraction={configuration.risk.risk_per_trade}, "
            f"max_gross_exposure={configuration.risk.max_gross_exposure}, "
            f"max_leverage={configuration.risk.max_leverage}, "
            "market_impact=nonlinear_volume_participation_model_with_2_percent_cap, "
            "maximum_round_trip_cost_r="
            f"{configuration.strategy.validation.maximum_estimated_round_trip_cost_r}.\n"
            f"Recent development failures: "
            f"{json.dumps(failure_digest, ensure_ascii=False, sort_keys=True)}\n"
            f"Rejected fingerprints: {json.dumps(rejected_fingerprints, ensure_ascii=False)}\n"
            "Return exactly one StrategySpec object. Set source to ai and explain the hypothesis "
            "briefly. Keep every parameter inside the schema limits. Favor lower turnover and "
            "wider economically feasible stops over cosmetic parameter variation."
        )
        mode = ai_view.configuration.routing_mode
        providers: list[AIProvider | None]
        if request.provider:
            providers = [request.provider]
            mode = "manual"
        else:
            # Research generation is the only automatic-AI boundary. One market
            # event produces one structured proposal; deterministic review owns
            # validation and comparison, so a second model call adds cost without
            # adding execution authority.
            providers = [None]
            mode = "single_efficient"

        proposals: list[AIProposalItem] = []
        try:
            for provider in providers:
                completion = None
                attempted: list[AIProvider] = []
                spec = None
                last_validation_error: ValueError | None = None
                repair_payload: dict = {}
                repair_error = "provider response was not valid JSON"
                for attempt in range(2):
                    try:
                        completion, attempted = await self.ai_gateway.generate_json(
                            system=system_prompt,
                            prompt=(
                                user_prompt
                                if attempt == 0
                                else (
                                    "Repair the previous invalid strategy response. Return one "
                                    "compact StrategySpec JSON object only, with every required "
                                    f"field inside its bound. Validation error: {repair_error}. "
                                    "Previous response: "
                                    + json.dumps(repair_payload, ensure_ascii=False, sort_keys=True)
                                )
                            ),
                            schema=strategy_spec_json_schema(),
                            schema_name="strategy_spec",
                            requested_provider=provider,
                            max_tokens=4000 if attempt == 0 else 6000,
                            # Reliability is more important than hidden chain-of-thought for
                            # a schema-constrained hypothesis. Deterministic review owns the
                            # empirical judgment after this isolated proposal is persisted.
                            thinking_enabled=attempt == 0,
                            operation="strategy_candidate_generation",
                            prompt_version="strategy_candidate_v4_cost_aware",
                            schema_version="strategy_spec_v1",
                        )
                        raw_spec = {
                            **completion.data,
                            "source": "ai",
                            "hypothesis": request.hypothesis,
                        }
                        spec = validate_strategy_spec(raw_spec, expected_kind=request.family)
                        break
                    except AIProviderError as exc:
                        if attempt == 0 and exc.code == AIErrorCode.INVALID_RESPONSE:
                            repair_error = str(exc)[:500]
                            continue
                        raise
                    except ValueError as exc:
                        last_validation_error = exc
                        repair_payload = dict(completion.data) if completion else {}
                        repair_error = str(exc)[:500]
                        if attempt == 0:
                            continue
                        raise
                if completion is None or spec is None:
                    raise last_validation_error or ValueError(
                        "AI strategy candidate repair did not produce a valid StrategySpec"
                    )
                spec_payload = spec.model_dump(mode="json", exclude={"fingerprint"})
                trial_group = stable_fingerprint(
                    {
                        "exchange": configuration.exchange_id,
                        "market_type": str(configuration.market_type),
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "family": spec_payload["family"],
                        "contract": "strategy_generation_v2",
                    }
                )
                context = CandidateResearchContext(
                    exchange=configuration.exchange_id,
                    market_type=str(configuration.market_type),
                    symbol=symbol,
                    timeframe=timeframe,
                    entry_profile=DEFAULT_ENTRY_PROFILE_ID,
                    market_as_of=market.timestamp,
                    summary_fingerprint=stable_fingerprint(summary),
                    fee_bps=configuration.fee_bps,
                    slippage_bps=configuration.slippage_bps,
                    risk_fraction=configuration.risk.risk_per_trade,
                    max_gross_exposure=configuration.risk.max_gross_exposure,
                    max_leverage=configuration.risk.max_leverage,
                    required_features=self.strategy_required_features(spec_payload),
                    trial_group=trial_group,
                    trial_count=await asyncio.to_thread(
                        self.candidate_trial_count,
                        trial_group=trial_group,
                    ),
                )
                preflight = self.build_candidate_preflight(
                    spec,
                    origin_summary=summary["timeframes"][timeframe],
                    configuration=configuration,
                )
                preflight_reasons = list(preflight.get("reasons") or [])
                if spec.fingerprint in rejected_fingerprints:
                    preflight["eligible"] = False
                    preflight_reasons.append("策略指纹与同一研究域的历史拒绝候选重复")
                    preflight["reasons"] = preflight_reasons
                candidate = create_candidate(spec, research_context=context)
                if not bool(preflight["eligible"]):
                    candidate.status = CandidateStatus.REJECTED
                    candidate.decision_reasons.extend(preflight_reasons)
                await asyncio.to_thread(self.store.save_strategy_candidate, candidate)
                if bool(preflight["eligible"]):
                    self.review_worker_wakeup.set()
                proposals.append(
                    AIProposalItem(
                        provider=completion.provider,
                        model=completion.model,
                        candidate=candidate,
                        usage=completion.usage,
                        latency_ms=completion.latency_ms,
                        route=attempted,
                        preflight=preflight,
                    )
                )
        except AIProviderError as exc:
            raise HTTPException(502, {"code": exc.code, "message": exc.public_message}) from exc
        except ValueError as exc:
            raise HTTPException(422, f"AI 策略候选未通过 StrategySpec 校验：{exc}") from exc

        self.add_audit(
            "research",
            "AI 已生成隔离的策略研究候选；候选尚不可执行",
            candidate_ids=",".join(item.candidate.id for item in proposals),
            providers=",".join(str(item.provider) for item in proposals),
            mode=mode,
        )
        return AIProposalResponse(mode=mode, proposals=proposals)
