from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from typing import Any


class ResearchCacheKind(StrEnum):
    ROLLING_FEATURE_CUBE = "rolling_feature_cube"
    SIGNAL_CACHE = "signal_cache"
    COST_SCENARIO_CACHE = "cost_scenario_cache"
    BOOTSTRAP_REFERENCE_CACHE = "bootstrap_reference_cache"


class ContentAddressedResearchCache:
    """Cache namespace whose key must be a complete ArtifactFingerprintV2 hash."""

    def __init__(self, store: Any, kind: ResearchCacheKind) -> None:
        self.store = store
        self.kind = kind

    def get(self, artifact_fingerprint: str) -> dict | None:
        return self.store.load_research_artifact(
            self._storage_key(artifact_fingerprint),
            str(self.kind),
        )

    def put(self, artifact_fingerprint: str, payload: dict) -> None:
        self.store.put_research_artifact(
            artifact_fingerprint=self._storage_key(artifact_fingerprint),
            artifact_kind=str(self.kind),
            payload=payload,
        )

    def _storage_key(self, artifact_fingerprint: str) -> str:
        return sha256(f"{self.kind}:{artifact_fingerprint}".encode()).hexdigest()


class RollingFeatureCube(ContentAddressedResearchCache):
    def __init__(self, store: Any) -> None:
        super().__init__(store, ResearchCacheKind.ROLLING_FEATURE_CUBE)


class SignalCache(ContentAddressedResearchCache):
    def __init__(self, store: Any) -> None:
        super().__init__(store, ResearchCacheKind.SIGNAL_CACHE)


class CostScenarioCache(ContentAddressedResearchCache):
    def __init__(self, store: Any) -> None:
        super().__init__(store, ResearchCacheKind.COST_SCENARIO_CACHE)


class BootstrapReferenceCache(ContentAddressedResearchCache):
    def __init__(self, store: Any) -> None:
        super().__init__(store, ResearchCacheKind.BOOTSTRAP_REFERENCE_CACHE)


class BehaviorFingerprintIndex:
    def __init__(self, store: Any) -> None:
        self.store = store

    def register(
        self,
        *,
        behavior_fingerprint: str,
        ir_hash: str,
        family_id: str,
        payload: dict,
    ) -> bool:
        """Return False for semantic duplicates, including after process restart."""
        return self.store.register_behavior_fingerprint(
            behavior_fingerprint=behavior_fingerprint,
            ir_hash=ir_hash,
            family_id=family_id,
            payload=payload,
        )
