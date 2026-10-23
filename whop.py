import os
from dotenv import load_dotenv
from whop_sdk import Whop
from whop_sdk.invoices.types.create_invoices_request_product import CreateInvoicesRequestProduct
from whop_sdk.invoices.types.create_invoices_request_product_line_items_item import CreateInvoicesRequestProductLineItemsItem
from whop_sdk.invoices.types.create_invoices_request_product_plan import CreateInvoicesRequestProductPlan
from whop_sdk.invoices.types.create_invoices_request_product_product import CreateInvoicesRequestProductProduct

load_dotenv()

WHOP_BASE_URL = os.getenv("WHOP_BASE_URL", "https://sandbox-api.whop.com/api/v1")


def get_whop_client() -> Whop | None:
    api_key = os.getenv("WHOP_API_KEY")
    if not api_key:
        return None
    return Whop(token=api_key, base_url=WHOP_BASE_URL)


client = get_whop_client()


def get_customer_emails(account_id: str | None = None):
    if client is None:
        print("WHOP_API_KEY is not configured")
        return []
    try:
        memberships = client.memberships.list(account_id=account_id or os.getenv("WHOP_ACCOUNT_ID", "biz_LfLkh5acNNVNWI"))
        result = []
        for sub in memberships:
            member_id = getattr(sub, "member_id", None)
            user_id = getattr(sub, "user_id", None)
            status = getattr(sub, "status", None)
            product_title = getattr(getattr(sub, "product", None), "title", "N/A")
            user_email = "Nema emaila"
            if user_id:
                try:
                    user_info = client.users.retrieve(user_id)
                    user_email = getattr(user_info, "email", None) or getattr(user_info, "username", None) or "N/A"
                except Exception as u_err:
                    user_email = f"Greška pri dohvatanju korisnika ({u_err})"
            print(f"Member ID: {member_id} | User ID: {user_id} | Email: {user_email} | Proizvod: {product_title} | Status: {status}")
            result.append({
                "member_id": member_id,
                "user_id": user_id,
                "email": user_email,
                "product_title": product_title,
                "status": status,
            })
        return result
    except Exception as e:
        print("Greška pri dohvatanju članova:", e)
        return []


def create_usage_overage_invoice(
    *,
    member_id: str | None,
    account_id: str | None,
    payment_method_id: str | None,
    extra_minutes: int,
    unit_price_usd: float,
    product_title: str = "Retell usage overage",
    description: str = "Retell overage minutes",
):
    if not member_id or not account_id:
        return {"status": "skipped", "reason": "missing_member_or_account"}
    if client is None:
        return {"status": "skipped", "reason": "WHOP_API_KEY_missing"}
    if extra_minutes <= 0:
        return {"status": "skipped", "reason": "no_overage"}

    total_amount = round(float(extra_minutes) * float(unit_price_usd), 2)
    payload = CreateInvoicesRequestProduct(
        account_id=account_id,
        collection_method="charge_automatically" if payment_method_id else "send_invoice",
        member_id=member_id,
        payment_method_id=payment_method_id,
        product=CreateInvoicesRequestProductProduct(title=product_title),
        plan=CreateInvoicesRequestProductPlan(
            currency="USD",
            description=description,
            renewal_price=float(total_amount),
        ),
        line_items=[
            CreateInvoicesRequestProductLineItemsItem(
                label="Retell extra minutes",
                quantity=float(extra_minutes),
                unit_price=float(unit_price_usd),
            )
        ],
    )

    try:
        invoice = client.invoices.create(request=payload)
        return {"status": "created", "invoice_id": getattr(invoice, "id", None), "amount_usd": total_amount}
    except Exception as exc:
        print(f"[WHOP] Failed to create overage invoice: {exc}")
        return {"status": "error", "detail": str(exc)}


if __name__ == "__main__":
    get_customer_emails()