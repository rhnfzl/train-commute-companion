"""Own Telegram proof, cursor and review transitions independently of journey progress."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .commands import ArrivalInput


class ProofPhase(StrEnum):
    SENDING = "sending"
    AWAITING = "awaiting"
    SAVING = "saving"
    CONFIRMED = "confirmed"
    FAILED = "failed"


@dataclass(slots=True)
class DeliveryProof:
    nonce: str
    phase: ProofPhase = ProofPhase.SENDING

    def arm(self) -> None:
        if self.phase is not ProofPhase.SENDING:
            raise RuntimeError("only a completed test pair can await confirmation")
        self.phase = ProofPhase.AWAITING

    def claim(self, nonce: str) -> bool:
        if self.phase is not ProofPhase.AWAITING or not secrets.compare_digest(
            nonce.encode(), self.nonce.encode()
        ):
            return False
        self.phase = ProofPhase.SAVING
        return True

    def saved(self, accepted: bool) -> None:
        if self.phase is not ProofPhase.SAVING:
            raise RuntimeError("no delivery proof is waiting for a save")
        self.phase = ProofPhase.CONFIRMED if accepted else ProofPhase.AWAITING


class CursorPhase(StrEnum):
    UNTRUSTED = "untrusted"
    READY = "ready"
    SAVING = "saving"
    ACKNOWLEDGING = "acknowledging"
    CLOSED = "closed"


@dataclass(slots=True)
class UpdateCursor:
    phase: CursorPhase

    @property
    def closed(self) -> bool:
        return self.phase is CursorPhase.CLOSED

    def begin(self) -> None:
        if self.phase not in (CursorPhase.UNTRUSTED, CursorPhase.READY):
            raise RuntimeError("the update cursor already owns a batch")
        self.phase = CursorPhase.SAVING

    def awaiting_ack(self) -> None:
        if self.phase is not CursorPhase.SAVING:
            raise RuntimeError("only an unwritten batch needs remote acknowledgement")
        self.phase = CursorPhase.ACKNOWLEDGING

    def release(self) -> bool:
        if self.phase is CursorPhase.CLOSED:
            return False
        if self.phase not in (CursorPhase.SAVING, CursorPhase.ACKNOWLEDGING):
            raise RuntimeError("the update cursor owns no batch")
        self.phase = CursorPhase.READY
        return True


class SavePhase(StrEnum):
    READY = "ready"
    FAILED = "failed"


class ReviewPhase(StrEnum):
    READY = "ready"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ArrivalDraft:
    arrival: ArrivalInput
    revision: int
    created_at: datetime

    def phase_at(self, now: datetime, lifetime: timedelta) -> ReviewPhase:
        age = now.astimezone(UTC) - self.created_at.astimezone(UTC)
        return ReviewPhase.READY if timedelta() <= age < lifetime else ReviewPhase.EXPIRED
