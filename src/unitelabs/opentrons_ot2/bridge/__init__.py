"""Contract-pinned adapter between external ``robot.*`` workflows and OT-2 SiLA."""

from .adapter import OT2StepError, OT2WorkflowAdapter
from .executor import OT2BridgeExecutor, OT2PreflightError
from .models import (
    EndpointKind,
    ExecutionMode,
    ExecutionRecord,
    OT2Command,
    PreflightExpectation,
    PreflightReport,
    WorkflowStep,
)
from .registry import ContractRegistry, EndpointBinding
from .transport import OT2SiLATransport
from .workflow import ParsedWorkflow, WorkflowFormatError, parse_workflow

__all__ = [
    "ContractRegistry",
    "EndpointBinding",
    "EndpointKind",
    "ExecutionMode",
    "ExecutionRecord",
    "OT2BridgeExecutor",
    "OT2Command",
    "OT2PreflightError",
    "OT2SiLATransport",
    "OT2StepError",
    "OT2WorkflowAdapter",
    "ParsedWorkflow",
    "PreflightExpectation",
    "PreflightReport",
    "WorkflowFormatError",
    "WorkflowStep",
    "parse_workflow",
]
