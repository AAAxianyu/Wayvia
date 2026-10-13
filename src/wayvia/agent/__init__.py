from wayvia.agent.client import AgentClient
from wayvia.agent.models import Action, Plan, Probe, State
from wayvia.agent.planner import build_plan

__all__ = [
    "Action",
    "AgentClient",
    "Plan",
    "Probe",
    "State",
    "TransactionManager",
    "build_plan",
    "serve_agent",
]


def __getattr__(name):
    if name == "serve_agent":
        from wayvia.agent.server import serve_agent

        return serve_agent
    if name == "TransactionManager":
        from wayvia.agent.transactions import TransactionManager

        return TransactionManager
    raise AttributeError(name)
