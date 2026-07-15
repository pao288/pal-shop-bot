import os
import aiohttp

class BankGateway:
    def __init__(self):
        self.base_url = os.getenv("BANK_API_URL", "").rstrip("/")
        self.api_key = os.getenv("BANK_API_KEY", "")

    async def request(self, operation: str, payload: dict) -> dict:
        if not self.base_url:
            return {"status": "FAILED", "error_code": "BANK_NOT_CONFIGURED"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(
                f"{self.base_url}/bank/operations",
                json={"operation": operation, **payload},
                timeout=aiohttp.ClientTimeout(total=20)
            ) as response:
                data = await response.json()
                return data

    async def reserve_purchase(self, transaction_id, buyer_id, seller_id, amount):
        return await self.request("SHOP_PURCHASE_RESERVE", {
            "transaction_id": str(transaction_id),
            "buyer_id": str(buyer_id),
            "seller_id": str(seller_id) if seller_id else None,
            "amount": amount,
            "currency": "PAL"
        })

    async def complete_transaction(self, transaction_id):
        return await self.request("SHOP_TRANSACTION_COMPLETE", {
            "transaction_id": str(transaction_id)
        })

    async def refund_transaction(self, transaction_id):
        return await self.request("SHOP_TRANSACTION_REFUND", {
            "transaction_id": str(transaction_id)
        })
