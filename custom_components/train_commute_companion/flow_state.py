"""A flow may save only the candidate its latest validation or review accepted."""

from dataclasses import dataclass
from enum import StrEnum


class ReviewPhase(StrEnum):
    SELECTING = "selecting"
    EDITING = "editing"
    REVIEWING = "reviewing"
    COMMITTING = "committing"
    COMPLETE = "complete"


class ProofPhase(StrEnum):
    UNTESTED = "untested"
    TESTING = "testing"
    REVIEWED = "reviewed"
    COMMITTING = "committing"
    COMPLETE = "complete"


@dataclass(slots=True)
class FlowProof:
    phase: ProofPhase = ProofPhase.UNTESTED
    identity: object = None
    version: int = 0

    def begin(self, identity: object) -> int:
        if self.phase in (ProofPhase.COMMITTING, ProofPhase.COMPLETE):
            raise ValueError("a claimed flow candidate cannot be replaced")
        self.version += 1
        self.identity = identity
        self.phase = ProofPhase.TESTING
        return self.version

    def accept(self, version: int, identity: object) -> bool:
        if (
            self.phase is not ProofPhase.TESTING
            or version != self.version
            or identity != self.identity
        ):
            return False
        self.phase = ProofPhase.REVIEWED
        return True

    def claim(self, identity: object) -> bool:
        if self.phase is not ProofPhase.REVIEWED or identity != self.identity:
            return False
        self.phase = ProofPhase.COMMITTING
        return True

    def retry(self) -> None:
        if self.phase is ProofPhase.COMMITTING:
            self.phase = ProofPhase.REVIEWED

    def finish(self) -> None:
        if self.phase is not ProofPhase.COMMITTING:
            raise RuntimeError("flow candidate has not been claimed")
        self.phase = ProofPhase.COMPLETE
