"""Adapter interfaces. Every outside system is reached only through one of these.

The core never imports a vendor SDK. A product swaps Frappe for Zendesk, or
Discord for Slack, by writing one class that satisfies the matching Protocol
(see docs/ADAPTERS.md). Every write method takes an ``idempotency_key``: a
retried call with the same key must not act twice.

Read-back methods exist because a successful write call is NOT proof. The
loop only records an action as done after reading the effect back from the
system that owns it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    ticket: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    body: str
    received_at: str  # ISO 8601 with timezone


@dataclass(frozen=True)
class SentRecord:
    ticket: str
    recipient: str
    sender: str
    subject: str
    body_sha: str
    message_id: str
    status: str  # "Sent" only when the provider says it left the building


@dataclass(frozen=True)
class PollResult:
    votes: dict[str, str]  # user id -> "yes" | "no"
    closed: bool = False


@dataclass(frozen=True)
class MoneyReceipt:
    kind: str  # "refund" | "cancel_subscription"
    provider_id: str
    amount: int | None
    currency: str | None
    status: str  # provider status, e.g. "succeeded" / "canceled"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str  # "open" | "merged" | "closed"
    head_sha: str
    url: str


@runtime_checkable
class Board(Protocol):
    """The human work surface: one card per case, tags as stages, polls as votes."""

    def ensure_card(self, idempotency_key: str, title: str, first_message: str, tags: list[str]) -> str: ...
    def get_tags(self, card_id: str) -> list[str]: ...
    def set_tags(self, card_id: str, tags: list[str]) -> None: ...
    def post(self, card_id: str, text: str, idempotency_key: str) -> str: ...
    def messages(self, card_id: str) -> list[str]: ...
    def open_poll(self, card_id: str, question: str, idempotency_key: str) -> str: ...
    def read_poll(self, card_id: str, poll_id: str) -> PollResult: ...
    def alert(self, text: str, idempotency_key: str) -> None: ...
    def notify(self, text: str, idempotency_key: str, mention: str | None = None) -> None:
        """A decision notice. Ping ``mention`` (one user id) and NOBODY else: no
        @everyone, no roles, no other users named in the text."""
        ...


@runtime_checkable
class Helpdesk(Protocol):
    """Owns customer correspondence. The loop never keeps a competing history."""

    def fetch_inbound(self, cursor: str | None) -> tuple[list[InboundMessage], str | None]: ...
    def send_reply(self, ticket: str, recipient: str, subject: str, body: str, idempotency_key: str) -> str: ...
    def sent_readback(self, ticket: str, body_sha: str) -> list[SentRecord]: ...
    def last_sent_at(self, ticket: str) -> str | None: ...
    def last_inbound_at(self, ticket: str, customer: str) -> str | None:
        """Newest inbound on this ticket OR from this customer on ANY ticket.

        A customer who replies by starting a new thread must still stop a close."""
        ...


@runtime_checkable
class Mailbox(Protocol):
    """The mailbox's own Sent folder: the independent receipt for an email."""

    def sent_copies(self, message_id: str) -> int: ...


@runtime_checkable
class Payments(Protocol):
    def refund(self, charge: str, amount: int, currency: str, idempotency_key: str) -> str: ...
    def cancel_subscription(self, subscription: str, idempotency_key: str) -> str: ...
    def read_refunds(self, charge: str) -> list[MoneyReceipt]: ...
    def read_subscription(self, subscription: str) -> MoneyReceipt: ...


@dataclass(frozen=True)
class PrFacts:
    """What the pre-QA gate reads about one exact head."""
    ci: str                          # "green" | "pending" | "red"
    files: tuple[str, ...]           # changed paths; empty = unreadable
    added: tuple[str, ...] | None    # added diff lines; None = unreadable


@runtime_checkable
class CodeHost(Protocol):
    def pull_request(self, number: int) -> PullRequest: ...
    def pr_facts(self, number: int, head: str) -> PrFacts: ...
    def merge(self, number: int, expected_head: str, idempotency_key: str) -> None: ...


@runtime_checkable
class Drafter(Protocol):
    """Writes a draft reply. Its output is data: it is never sent unapproved."""

    def draft(self, case: dict[str, Any], thread: list[str]) -> str: ...


@runtime_checkable
class DevRunner(Protocol):
    """Starts a bounded coding run from a brief and reports its PR number."""

    def start(self, case_key: str, brief: str, idempotency_key: str) -> str: ...
    def result(self, run_id: str) -> dict[str, Any] | None: ...


@runtime_checkable
class QaRunner(Protocol):
    """Independent QA of one exact PR head. It reports; it never merges.

    ``fails_on_old_code`` returns {"state": "red-proved" | "not-red" | "head-red" | "error", "reason": str}.
    ``result`` returns None while running, else {"text": <full QA output>, "usage": {...} optional}.
    """

    def fails_on_old_code(self, pr: int, head: str) -> dict[str, Any]: ...
    def start(self, case_key: str, brief: str, idempotency_key: str) -> str: ...
    def result(self, run_id: str) -> dict[str, Any] | None: ...
