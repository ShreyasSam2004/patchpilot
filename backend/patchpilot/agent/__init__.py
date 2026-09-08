"""The autonomous agent loop."""

from .antiloop import LoopDetector, LoopSignal, canonical_action, normalize_patch
from .budget import Budget, BudgetTracker
from .orchestrator import AgentOrchestrator, AgentTask
from .result import (
    AgentResult,
    HypothesisRecord,
    PatchAttemptRecord,
    RunMetrics,
    TestRunRecord,
    ToolCallRecord,
)
from .session import SessionConfig, agent_session, build_registry, run_agent_task
from .transcript import Transcript

__all__ = [
    "AgentOrchestrator",
    "AgentResult",
    "AgentTask",
    "Budget",
    "BudgetTracker",
    "HypothesisRecord",
    "LoopDetector",
    "LoopSignal",
    "PatchAttemptRecord",
    "RunMetrics",
    "SessionConfig",
    "TestRunRecord",
    "ToolCallRecord",
    "Transcript",
    "agent_session",
    "build_registry",
    "canonical_action",
    "normalize_patch",
    "run_agent_task",
]
