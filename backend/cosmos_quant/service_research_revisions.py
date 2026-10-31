from __future__ import annotations

import asyncio
from decimal import Decimal

from fastapi import HTTPException

from .api_contracts import (
    CampaignRevisionRequest,
    CandidateRevisionResponse,
    CausalPolicyCampaignSpec,
    CausalPolicyRevisionRequest,
    CausalPolicyRevisionResponse,
)
from .research_campaign import ResearchCampaignReport
from .research_hypothesis import generate_campaign_failure_revisions
from .research_lifecycle import create_candidate
from .strategy_library import library_entry

logger = __import__("logging").getLogger(__name__)


class ResearchRevisionsService:
    async def revise_campaign_strategy(
        self,
        campaign_id: str,
        request: CampaignRevisionRequest,
    ):
        payload = await asyncio.to_thread(
            self.store.load_research_campaign,
            campaign_id,
        )
        if not payload:
            raise HTTPException(404, "research campaign not found")
        report = ResearchCampaignReport.model_validate(payload)
        if report.blind_consumption_id:
            raise HTTPException(
                409,
                "a consumed blind Campaign is terminal evidence; retire the hypothesis "
                "lineage and use a newly sealed holdout for any independent successor",
            )
        if report.parent_campaign_id is not None:
            raise HTTPException(
                409,
                "a bounded successor cannot generate a second revision generation",
            )
        if report.authorized_revision_candidate_ids:
            raise HTTPException(
                409,
                "this Campaign has already frozen its one permitted revision generation",
            )
        lineage_history = await asyncio.to_thread(self.store.list_research_campaigns, 1000)
        if any(item.get("parent_campaign_id") == report.id for item in lineage_history):
            raise HTTPException(
                409,
                "this Campaign already has its one permitted successor",
            )
        aggregate = (
            next(
                (
                    item
                    for item in report.candidates
                    if item.library_entry_id == request.library_entry_id
                ),
                None,
            )
            if request.library_entry_id
            else report.candidates[0]
            if report.candidates
            else None
        )
        if aggregate is None:
            raise HTTPException(404, "campaign strategy aggregate not found")
        if aggregate.library_entry_id.startswith("candidate:"):
            base_candidate_id = aggregate.library_entry_id.removeprefix("candidate:")
        else:
            base_candidate_id = f"cand_{aggregate.library_entry_id}"
        candidate_payload = await asyncio.to_thread(
            self.store.load_strategy_candidate,
            base_candidate_id,
        )
        candidate = (
            self.parse_strategy_candidate_payload(
                candidate_payload,
                audit_context="campaign_failure_revision",
            )
            if candidate_payload
            else None
        )
        if candidate is None:
            if aggregate.library_entry_id.startswith("candidate:"):
                raise HTTPException(409, "campaign candidate is stale or missing")
            try:
                entry = library_entry(aggregate.library_entry_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            candidate = create_candidate(entry.validated_spec)
            candidate.id = base_candidate_id
            await asyncio.to_thread(self.store.save_strategy_candidate, candidate)
        cells = [
            cell for cell in report.cells if cell.library_entry_id == aggregate.library_entry_id
        ]
        attribution, revisions = generate_campaign_failure_revisions(
            candidate,
            aggregate,
            cells,
            max_revisions=request.max_revisions,
        )
        for revision in revisions:
            await asyncio.to_thread(
                self.store.save_strategy_candidate,
                revision.candidate,
            )
        report = report.model_copy(
            update={
                "authorized_revision_candidate_ids": [
                    revision.candidate.id for revision in revisions
                ],
                "authorized_revision_fingerprints": [
                    revision.candidate.spec_fingerprint for revision in revisions
                ],
                "revision_source_library_entry_id": aggregate.library_entry_id,
            }
        )
        await asyncio.to_thread(self.store.save_research_campaign, report)
        self.add_audit(
            "research",
            "Campaign 聚合失败已生成单变量策略修订",
            campaign_id=campaign_id,
            library_entry_id=aggregate.library_entry_id,
            parent_candidate_id=candidate.id,
            failure_codes=",".join(attribution.codes),
            revision_count=len(revisions),
        )
        return CandidateRevisionResponse(
            parent_candidate_id=candidate.id,
            attribution=attribution,
            revisions=revisions,
        )

    async def revise_campaign_causal_policy(
        self,
        campaign_id: str,
        request: CausalPolicyRevisionRequest,
    ) -> CausalPolicyRevisionResponse:
        payload = await asyncio.to_thread(self.store.load_research_campaign, campaign_id)
        if not payload:
            raise HTTPException(404, "research campaign not found")
        report = ResearchCampaignReport.model_validate(payload)
        if not report.causal_policy_definitions:
            raise HTTPException(422, "Campaign does not contain causal policy hypotheses")
        if report.blind_consumption_id:
            raise HTTPException(409, "a consumed blind Campaign is terminal evidence")
        if report.parent_campaign_id is not None:
            raise HTTPException(409, "a bounded successor cannot create another generation")
        if report.authorized_policy_revision_fingerprints:
            raise HTTPException(
                409,
                "this Campaign already froze its one permitted causal policy revision generation",
            )
        lineage_history = await asyncio.to_thread(self.store.list_research_campaigns, 1000)
        if any(item.get("parent_campaign_id") == report.id for item in lineage_history):
            raise HTTPException(409, "this Campaign already has its one permitted successor")
        aggregate = (
            next(
                (
                    item
                    for item in report.candidates
                    if item.library_entry_id == request.campaign_entry_id
                ),
                None,
            )
            if request.campaign_entry_id
            else report.candidates[0]
            if report.candidates
            else None
        )
        if aggregate is None:
            raise HTTPException(404, "causal policy aggregate not found")
        definition = next(
            (
                item
                for item in report.causal_policy_definitions
                if item.get("campaign_entry_id") == aggregate.library_entry_id
            ),
            None,
        )
        if definition is None:
            raise HTTPException(409, "causal policy definition is missing from the Campaign")
        parent_spec = CausalPolicyCampaignSpec(
            id=str(definition["policy_id"]),
            library_entry_ids=[str(item["id"]) for item in definition["priors"]],
            maximum_candidates_per_bar=int(definition["maximum_candidates_per_bar"]),
            abstain_on_ambiguous_regime=bool(definition["abstain_on_ambiguous_regime"]),
            minimum_regime_confidence=Decimal(
                str(definition.get("minimum_regime_confidence", "0"))
            ),
            minimum_signal_confidence=Decimal(
                str(definition.get("minimum_signal_confidence", "0"))
            ),
            minimum_directional_consensus=int(definition.get("minimum_directional_consensus", 1)),
        )
        proposed_changes: list[tuple[str, object, str]] = []
        next_signal_confidence = min(
            parent_spec.minimum_signal_confidence + Decimal("0.10"),
            Decimal("0.90"),
        )
        if next_signal_confidence != parent_spec.minimum_signal_confidence:
            proposed_changes.append(
                ("minimum_signal_confidence", next_signal_confidence, "signal_confidence")
            )
        next_regime_confidence = min(
            parent_spec.minimum_regime_confidence + Decimal("0.10"),
            Decimal("0.90"),
        )
        if next_regime_confidence != parent_spec.minimum_regime_confidence:
            proposed_changes.append(
                ("minimum_regime_confidence", next_regime_confidence, "regime_confidence")
            )
        if parent_spec.maximum_candidates_per_bar < 20:
            proposed_changes.append(
                (
                    "maximum_candidates_per_bar",
                    parent_spec.maximum_candidates_per_bar + 1,
                    "candidate_breadth",
                )
            )
        revisions: list[CausalPolicyCampaignSpec] = []
        for field_name, value, label in proposed_changes[: request.max_revisions]:
            token = str(value).replace(".", "")
            revision = parent_spec.model_copy(
                update={
                    "id": f"{parent_spec.id}_{label}_{token}",
                    field_name: value,
                }
            )
            changed_fields = {
                key
                for key, current in parent_spec.model_dump(mode="json").items()
                if key != "id" and revision.model_dump(mode="json")[key] != current
            }
            if changed_fields != {field_name}:
                raise RuntimeError("causal policy revision must change exactly one control")
            revisions.append(CausalPolicyCampaignSpec.model_validate(revision))
        if not revisions:
            raise HTTPException(409, "causal policy has no remaining bounded revision")
        frozen_entries = [self.build_causal_policy_campaign_entry(item) for item in revisions]
        report = report.model_copy(
            update={
                "authorized_policy_revision_definitions": [
                    item.frozen_definition for item in frozen_entries
                ],
                "authorized_policy_revision_fingerprints": [
                    item.policy_fingerprint for item in frozen_entries
                ],
                "revision_source_library_entry_id": aggregate.library_entry_id,
            }
        )
        await asyncio.to_thread(self.store.save_research_campaign, report)
        self.add_audit(
            "research",
            "Campaign failure froze one bounded causal policy revision generation",
            campaign_id=campaign_id,
            campaign_entry_id=aggregate.library_entry_id,
            parent_policy_fingerprint=str(definition["policy_fingerprint"]),
            revision_count=len(revisions),
        )
        return CausalPolicyRevisionResponse(
            parent_campaign_entry_id=aggregate.library_entry_id,
            parent_policy_fingerprint=str(definition["policy_fingerprint"]),
            failure_reasons=aggregate.reasons,
            revisions=revisions,
        )
