import os
from typing import Protocol

from app.services import paytech


class PaymentGateway(Protocol):
    name: str

    def configured(self) -> bool:
        ...

    async def create_topup_payment(
        self,
        user_id: str,
        amount: float,
        phone: str,
        full_name: str,
        reference_id: str,
    ) -> dict:
        ...

    async def get_payment_status(self, payment_id: str) -> dict:
        ...

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        ...

    def parse_webhook(self, body: dict) -> dict:
        ...


class PaytechGateway:
    name = "paytech"

    def configured(self) -> bool:
        return paytech.provider_configured()

    async def create_topup_payment(self, user_id: str, amount: float, phone: str, full_name: str, reference_id: str) -> dict:
        return await paytech.create_topup_payment(user_id, amount, phone, full_name, reference_id)

    async def get_payment_status(self, payment_id: str) -> dict:
        return await paytech.get_payment_status(payment_id)

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        return paytech.verify_webhook_signature(body, signature)

    def parse_webhook(self, body: dict) -> dict:
        return paytech.parse_webhook(body)


class UnsupportedGateway:
    def __init__(self, name: str):
        self.name = name

    def configured(self) -> bool:
        return False

    async def create_topup_payment(self, *args, **kwargs) -> dict:
        raise Exception(f"{self.name} payment gateway adapter ulanmagan")

    async def get_payment_status(self, payment_id: str) -> dict:
        raise Exception(f"{self.name} payment gateway adapter ulanmagan")

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        return False

    def parse_webhook(self, body: dict) -> dict:
        raise Exception(f"{self.name} payment gateway adapter ulanmagan")


def get_payment_gateway() -> PaymentGateway:
    provider = os.getenv("PAYMENT_PROVIDER", "paytech").strip().lower()
    if provider in ("paytech", "payme", "payme_business"):
        return PaytechGateway()
    return UnsupportedGateway(provider)
