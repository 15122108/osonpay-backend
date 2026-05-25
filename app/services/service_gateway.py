import os
from typing import Protocol

from app.services import paysys


class ServiceGateway(Protocol):
    name: str

    def mobile_configured(self) -> bool:
        ...

    async def mobile_info(self, phone_number: str) -> dict:
        ...

    async def mobile_pay(self, phone_number: str, amount: float, payment_number: str) -> dict:
        ...

    async def mobile_status(self, transaction_id: str | int) -> dict:
        ...


class PaysysServiceGateway:
    name = "paysys"

    def mobile_configured(self) -> bool:
        return paysys.mobile_configured()

    async def mobile_info(self, phone_number: str) -> dict:
        return await paysys.mobile_info(phone_number)

    async def mobile_pay(self, phone_number: str, amount: float, payment_number: str) -> dict:
        return await paysys.mobile_pay(phone_number, amount, payment_number)

    async def mobile_status(self, transaction_id: str | int) -> dict:
        return await paysys.mobile_status(transaction_id)


class UnsupportedServiceGateway:
    def __init__(self, name: str):
        self.name = name

    def mobile_configured(self) -> bool:
        return False

    async def mobile_info(self, phone_number: str) -> dict:
        raise Exception(f"{self.name} service payment adapter ulanmagan")

    async def mobile_pay(self, phone_number: str, amount: float, payment_number: str) -> dict:
        raise Exception(f"{self.name} service payment adapter ulanmagan")

    async def mobile_status(self, transaction_id: str | int) -> dict:
        raise Exception(f"{self.name} service payment adapter ulanmagan")


def get_service_gateway() -> ServiceGateway:
    provider = os.getenv("SERVICE_PAYMENT_PROVIDER", "paysys").strip().lower()
    if provider in ("paysys", "paynet", "service_gateway"):
        return PaysysServiceGateway()
    return UnsupportedServiceGateway(provider)
