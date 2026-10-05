"""A scriptable stand-in for the custodial bank's payout API.

Never talks to a network. Each call pops the next scripted result (the last
one repeats); an Exception in the script is raised instead of returned.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.banking.gateway import InquiryOutcome, InquiryResult, SubmitOutcome, SubmitResult, TransferRequest


class FakeGateway:
    def __init__(
        self,
        submit: list[SubmitResult | Exception] | None = None,
        inquiry: list[InquiryResult | Exception] | None = None,
        submit_delay: float = 0.0,
    ):
        self._submit = list(submit or [SubmitResult(SubmitOutcome.ACCEPTED, bank_transaction_ref="BANKREF1")])
        self._inquiry = list(inquiry or [InquiryResult(InquiryOutcome.PENDING)])
        self.submit_delay = submit_delay
        self.submits: list[TransferRequest] = []
        self.inquiries: list[str] = []
        self.on_submit: Callable[[TransferRequest], Awaitable[None]] | None = None

    @staticmethod
    def _next(script: list):
        result = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(result, Exception):
            raise result
        return result

    async def submit_transfer(self, request: TransferRequest) -> SubmitResult:
        self.submits.append(request)
        if self.submit_delay:
            await asyncio.sleep(self.submit_delay)
        if self.on_submit is not None:
            await self.on_submit(request)
        return self._next(self._submit)

    async def inquire_transfer(self, client_request_id: str) -> InquiryResult:
        self.inquiries.append(client_request_id)
        return self._next(self._inquiry)
