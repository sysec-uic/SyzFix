"""
Base class for all analyzers.

Each analyzer implements analyze() which takes a list of BugEntry objects
and returns an AnalysisResult. This makes it easy to add new analyzers:
just subclass BaseAnalyzer and implement analyze().
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..loader import BugEntry


@dataclass
class AnalysisResult:
    """Result from an analyzer."""
    name: str
    summary: dict[str, Any] = field(default_factory=dict)
    details: list[dict[str, Any]] = field(default_factory=list)
    tables: dict[str, list[dict]] = field(default_factory=dict)

    def print_summary(self):
        """Print a human-readable summary."""
        print(f"\n{'=' * 70}")
        print(f"  {self.name}")
        print(f"{'=' * 70}")
        for key, value in self.summary.items():
            if isinstance(value, float):
                print(f"  {key}: {value:.1f}")
            else:
                print(f"  {key}: {value}")
        print()


class BaseAnalyzer(ABC):
    """Base class for dataset analyzers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for this analyzer."""
        ...

    @abstractmethod
    def analyze(self, bugs: list[BugEntry]) -> AnalysisResult:
        """Run the analysis and return results."""
        ...
