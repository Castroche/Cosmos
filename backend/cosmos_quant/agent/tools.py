from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from inspect import Parameter, signature
from typing import Any, get_type_hints

from pydantic import BaseModel, TypeAdapter

from .policy import AgentPolicy, AgentPolicyEngine
from .schemas import AgentObservation, AgentToolCall, ToolRiskClass
from .store import AgentStore

ToolHandler = Callable[..., dict[str, Any] | BaseModel | Awaitable[dict[str, Any] | BaseModel]]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    risk_class: ToolRiskClass
    handler: ToolHandler
    timeout_seconds: float = 30
    include_context: bool = False
    parameters: dict[str, Any] | None = None


class AgentToolRegistry:
    def __init__(self):
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Agent tool {tool.name} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AgentTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise LookupError(f"unknown Agent tool: {name}") from exc

    @staticmethod
    def _parameter_schema(tool: AgentTool) -> dict[str, Any]:
        if tool.parameters is not None:
            return tool.parameters
        handler_signature = signature(tool.handler)
        try:
            annotations = get_type_hints(tool.handler)
        except (NameError, TypeError):
            annotations = {}
        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, parameter in handler_signature.parameters.items():
            if name.startswith("_agent_") or parameter.kind in {
                Parameter.VAR_POSITIONAL,
                Parameter.VAR_KEYWORD,
            }:
                continue
            annotation = annotations.get(name, parameter.annotation)
            if annotation is Parameter.empty:
                property_schema: dict[str, Any] = {}
            else:
                property_schema = TypeAdapter(annotation).json_schema()
            if parameter.default is Parameter.empty:
                required.append(name)
            else:
                property_schema = {
                    **property_schema,
                    "default": parameter.default,
                }
            properties[name] = property_schema
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def descriptions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "risk_class": str(tool.risk_class),
                "parameters": self._parameter_schema(tool),
            }
            for tool in self._tools.values()
        ]


class AgentToolExecutor:
    def __init__(
        self,
        registry: AgentToolRegistry,
        store: AgentStore,
        policy_engine: AgentPolicyEngine | None = None,
    ):
        self.registry = registry
        self.store = store
        self.policy_engine = policy_engine or AgentPolicyEngine()

    async def execute(
        self,
        *,
        run_id: str,
        call: AgentToolCall,
        policy: AgentPolicy,
    ) -> AgentObservation:
        tool = self.registry.get(call.name)
        authorization = self.policy_engine.evaluate(policy, call, tool.risk_class)
        if not authorization.allowed:
            observation = AgentObservation(
                run_id=run_id,
                kind="policy_rejection",
                ok=False,
                summary="Agent tool call rejected by the non-LLM policy gate",
                data={"reasons": authorization.reasons, "tool": call.name},
                tool_call_id=call.id,
            )
            self.store.save_observation(observation)
            return observation

        status, existing, created = self.store.begin_tool_call(run_id, call)
        if not created:
            if status in {"completed", "failed"} and existing is not None:
                observation = AgentObservation.model_validate(existing)
                self.store.save_observation(observation)
                return observation
            observation = AgentObservation(
                run_id=run_id,
                kind="tool_uncertain",
                ok=False,
                summary="An identical tool call is already running; no duplicate was executed",
                data={"tool": call.name, "idempotency_key": call.idempotency_key},
                tool_call_id=call.id,
            )
            self.store.save_observation(observation)
            return observation

        try:
            arguments = dict(call.arguments)
            if tool.include_context:
                arguments.update(
                    {
                        "_agent_run_id": run_id,
                        "_agent_tool_call_id": call.id,
                    }
                )
            if asyncio.iscoroutinefunction(tool.handler):
                result = await asyncio.wait_for(
                    tool.handler(**arguments), timeout=tool.timeout_seconds
                )
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(tool.handler, **arguments),
                    timeout=tool.timeout_seconds,
                )
            if isinstance(result, BaseModel):
                data = result.model_dump(mode="json")
            elif isinstance(result, dict):
                data = result
            else:
                raise TypeError("Agent tools must return a dict or Pydantic model")
            observation = AgentObservation(
                run_id=run_id,
                kind="tool_result",
                ok=True,
                summary=f"Agent tool {call.name} completed",
                data=data,
                tool_call_id=call.id,
            )
            self.store.finish_tool_call(
                call,
                observation.model_dump(mode="json"),
                observation=observation,
            )
        except Exception as exc:
            observation = AgentObservation(
                run_id=run_id,
                kind="tool_error",
                ok=False,
                summary=f"Agent tool {call.name} failed: {str(exc)[:500]}",
                data={"error_type": type(exc).__name__},
                tool_call_id=call.id,
            )
            self.store.finish_tool_call(
                call,
                observation.model_dump(mode="json"),
                failed=True,
                observation=observation,
            )
        return observation
