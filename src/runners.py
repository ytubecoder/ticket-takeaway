"""Runner abstraction — agent / scenario / gap-analyzer.

M1a stub. M3 fills in the AgentRunner (extends the existing workflow_runs
plumbing) and ScenarioRunner (wraps the existing tests/scenario_runner.py).
M4 adds GapAnalyzer.

The Runner ABC is the single seam where Kitchen meets the actual work.
Each concrete runner reads a `runs` row, executes against the workspace,
streams events into activity_events, and updates the run status.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Runner(ABC):
    """Base class. M1a defines the shape; M3 implements concrete runners."""

    runner_kind: str  # 'agent' | 'scenario' | 'gap_analyzer'

    @abstractmethod
    def run(self, run_row, workspace_path: str) -> None:
        """Execute one run attempt. Status transitions live on the run_row.

        Implementations must:
          - update run.status (preparing → running → terminal) in the same DB
          - stream agent_output / hook_* / etc. as activity_events
          - never raise on terminal failure — set status='failed' instead
        """
        raise NotImplementedError
