"""
Embedly API client (https://developer.embedly.ng).

Embedly's services answer in different envelopes, and the HTTP status often
disagrees with the outcome (an unknown transfer is HTTP 400 with body code
"404"; an unknown payout is HTTP 200 with statusCode 404). Every call is
normalised into an EmbedlyResponse so callers only look at `.ok`, `.code`,
`.message` and `.data`.

Logging records method, path, status and message only — never bodies, which
carry BVN/NIN and account details.
"""
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# ISO 8583-style codes Embedly documents as "outcome not final yet".
AMBIGUOUS_CODES = {'01', '09', '91', '92', '96', '97'}


@dataclass
class EmbedlyResponse:
    ok: bool
    http_status: int
    code: str = ''
    message: str = ''
    data: Any = None
    raw: dict = field(default_factory=dict)
    transport_error: bool = False  # timeout / connection failure: outcome unknown

    @property
    def not_found(self):
        return self.code == '404' or self.http_status == 404

    @property
    def ambiguous(self):
        """True when money may or may not have moved; requery before retrying."""
        return self.transport_error or self.http_status >= 500 or self.code in AMBIGUOUS_CODES


def _normalise(http_status, body):
    if not isinstance(body, dict):
        return EmbedlyResponse(ok=200 <= http_status < 300, http_status=http_status, data=body)

    # Core v1:  {code, success, message, data}
    # Core v2:  {data, status, message}
    # Payout:   {data, statusCode, code, message, succeeded}
    # Create wallet (flat): {message, walletId, virtualAccount, ...}
    code = body.get('code')
    if code is None:
        inner = body.get('statusCode', body.get('status'))
        code = str(inner) if inner is not None else ''
    code = str(code)

    flags = [body[k] for k in ('success', 'succeeded') if isinstance(body.get(k), bool)]
    if flags:
        ok = all(flags)
    elif code and code not in ('00', '200', '201'):
        ok = False
    else:
        ok = 200 <= http_status < 300
    ok = ok and 200 <= http_status < 300

    data = body['data'] if 'data' in body else body
    return EmbedlyResponse(
        ok=ok, http_status=http_status, code=code,
        message=str(body.get('message') or body.get('title') or ''),
        data=data, raw=body,
    )


def _amount(value):
    """Embedly takes naira as a JSON number."""
    return float(Decimal(str(value)).quantize(Decimal('0.01')))


class EmbedlyClient:
    TIMEOUT = 30

    @property
    def core_url(self):
        return settings.EMBEDLY_BASE_URL.rstrip('/')  # .../api/v1

    @property
    def host_url(self):
        return self.core_url.rsplit('/api/', 1)[0]  # https://waas-<env>.embedly.ng

    @property
    def payout_url(self):
        return settings.EMBEDLY_PAYOUT_URL.rstrip('/')  # .../api

    def _request(self, method, url, payload=None, params=None):
        path = url.split('.embedly.ng', 1)[-1].split('?', 1)[0]
        try:
            response = requests.request(
                method, url, json=payload, params=params, timeout=self.TIMEOUT,
                headers={
                    'x-api-key': settings.EMBEDLY_API_KEY,
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("Embedly %s %s transport error: %s", method, path, exc.__class__.__name__)
            return EmbedlyResponse(
                ok=False, http_status=0, message=str(exc), transport_error=True,
            )

        try:
            body = response.json()
        except ValueError:
            body = {'message': response.text[:200]}
        result = _normalise(response.status_code, body)
        logger.info(
            "Embedly %s %s -> %s code=%s ok=%s msg=%s",
            method, path, response.status_code, result.code, result.ok, result.message[:120],
        )
        return result

    # -- customers ------------------------------------------------------------

    def create_individual_customer(self, *, first_name, last_name, middle_name, dob,
                                   address, city, email, mobile_number):
        return self._request('POST', f"{self.core_url}/customers/add", {
            'organizationId': settings.EMBEDLY_ORGANISATION_ID,
            'firstName': first_name,
            'lastName': last_name,
            'middleName': middle_name or '',
            'dob': dob.isoformat(),
            'customerTypeId': settings.EMBEDLY_CUSTOMER_TYPE_ID,
            'address': address,
            'city': city,
            'countryId': settings.EMBEDLY_COUNTRY_ID,
            'emailAddress': email,
            'mobileNumber': mobile_number or '',
        })

    def create_corporate_customer(self, *, rc_number, tin, business_name, address, city,
                                  email, wallet_name):
        return self._request('POST', f"{self.core_url}/corporate/customers", {
            'rcNumber': rc_number,
            'tin': tin,
            'fullBusinessName': business_name,
            'businessAddress': address,
            'countryId': settings.EMBEDLY_COUNTRY_ID,
            'city': city,
            'email': email,
            'walletPreferredName': wallet_name,
        })

    def get_corporate_customer(self, customer_id):
        """`.data['isCorporateVerified']` is the back-office approval state ("Pending", ...)."""
        return self._request('GET', f"{self.core_url}/corporate/customers/{customer_id}")

    def add_corporate_director(self, customer_id, *, first_name, last_name, middle_name, email, phone,
                               dob, address, bvn, nin, meter_number, disco_code):
        return self._request('POST', f"{self.core_url}/corporate/customers/{customer_id}/directors", {
            'firstName': first_name,
            'lastName': last_name,
            'middleName': middle_name or '',
            'email': email,
            'phoneNumber': phone,
            'dateOfBirth': dob.isoformat(),
            'address': address,
            'bvn': bvn,
            'nin': nin,
            'meterNumber': meter_number,
            'discoCode': disco_code,
        })

    def submit_corporate_documents(self, customer_id, urls, *, replace=False):
        """
        `urls` maps Embedly's document names (cac, tin, boardResolution,
        utilityBill, memart, scuml) to URLs from upload_file — the API takes
        URLs, not the base64 its docs describe. The first submission is a POST;
        a resubmission must be a PUT (a second POST answers 409).
        """
        return self._request(
            'PUT' if replace else 'POST',
            f"{self.core_url}/corporate/customers/{customer_id}/documents", urls,
        )

    def upload_file(self, filename, content, content_type):
        """Upload a KYC document (PDF/JPEG/PNG); `.data` is its URL."""
        try:
            response = requests.post(
                f"{self.core_url}/utilities/upload", timeout=60,
                headers={'x-api-key': settings.EMBEDLY_API_KEY, 'Accept': 'application/json'},
                files={'file': (filename, content, content_type)},
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("Embedly upload transport error: %s", exc.__class__.__name__)
            return EmbedlyResponse(ok=False, http_status=0, message=str(exc), transport_error=True)
        try:
            body = response.json()
        except ValueError:
            body = {'message': response.text[:200]}
        result = _normalise(response.status_code, body)
        logger.info("Embedly POST /utilities/upload -> %s ok=%s", response.status_code, result.ok)
        return result

    def verify_bvn(self, customer_id, bvn):
        return self._request(
            'POST', f"{self.core_url}/customers/kyc/premium-kyc", {'customerId': customer_id, 'bvn': bvn},
            params={'verify': 1},
        )

    def verify_nin(self, customer_id, nin, *, first_name, last_name, dob):
        return self._request(
            'POST', f"{self.core_url}/customers/kyc/customer/nin",
            {'firstname': first_name, 'lastname': last_name, 'dob': dob.isoformat()},
            params={'customerId': customer_id, 'nin': nin, 'verify': 1},
        )

    def initiate_liveness(self, customer_id, redirect_url=''):
        """
        Selfie check, required (after BVN/NIN) for a KYC tier. `.data` has
        `status` ("completed" — staging's mock finishes at once — or
        "in_progress") and `startKycUrl`, which the customer opens to take the
        selfie. The verdict arrives as a customer.liveness.* webhook.
        """
        payload = {'customerId': customer_id, 'submissionChannel': 'WAAS'}
        if redirect_url:
            payload['redirectUrl'] = redirect_url
        return self._request('POST', f"{self.core_url}/customers/kyc/liveliness", payload)

    # -- wallets --------------------------------------------------------------

    def create_individual_wallet(self, customer_id, name):
        return self._request('POST', f"{self.core_url}/wallets/add", {
            'customerId': customer_id,
            'currencyId': settings.EMBEDLY_CURRENCY_ID,
            'name': name,
        })

    def create_corporate_wallet(self, customer_id, name):
        return self._request(
            'POST', f"{self.core_url}/corporate/customers/{customer_id}/wallets",
            {'currencyId': settings.EMBEDLY_CURRENCY_ID, 'name': name},
        )

    def get_wallet_by_account(self, account_number):
        return self._request('GET', f"{self.core_url}/wallets/get/wallet/account/{account_number}")

    def get_account_history(self, account_number, start, end, page=1, page_size=50):
        return self._request('GET', f"{self.core_url}/wallets/account-number/history", params={
            'AccountNumber': account_number,
            'From': start.isoformat(),
            'To': end.isoformat(),
            'PageNumber': page,
            'PageSize': page_size,
        })

    def wallet_to_wallet(self, *, from_account, to_account, amount, reference, remarks=''):
        """Instant; a successful response means the money has moved."""
        return self._request('PUT', f"{self.core_url}/wallets/wallet/transaction/v2/wallet-to-wallet", {
            'fromAccount': from_account,
            'toAccount': to_account,
            'amount': _amount(amount),
            'transactionReference': reference,
            'remarks': (remarks or 'Shifta transfer')[:100],
        })

    def wallet_to_wallet_status(self, reference):
        return self._request(
            'GET', f"{self.core_url}/wallets/wallet/transaction/wallet-to-wallet/status/{reference}",
        )

    def simulate_inflow(self, account_number, account_name, amount, narration='Staging inflow'):
        """Staging only: pretend a bank transfer arrived. Fires the `nip` webhook."""
        return self._request('POST', f"{self.host_url}/WaasCore/api/v1/nip/inflow/simulate-inflow", {
            'beneficiaryAccountName': account_name,
            'beneficiaryAccountNumber': account_number,
            'amount': str(Decimal(str(amount)).quantize(Decimal('0.01'))),
            'narration': narration,
        })

    # -- payouts --------------------------------------------------------------

    def get_banks(self):
        return self._request('GET', f"{self.payout_url}/Payout/banks")

    def name_enquiry(self, account_number, bank_code):
        return self._request('POST', f"{self.payout_url}/Payout/name-enquiry", {
            'bankCode': bank_code, 'accountNumber': account_number,
        })

    def bank_payout(self, *, source_account, source_account_name, destination_account,
                    destination_bank_code, destination_account_name, amount, reference, remarks=''):
        """Asynchronous; returns Embedly's transactionRef in `.data`. Final status by webhook/requery."""
        payload = {
            'destinationBankCode': destination_bank_code,
            'destinationAccountNumber': destination_account,
            'destinationAccountName': destination_account_name,
            'sourceAccountNumber': source_account,
            'sourceAccountName': source_account_name,
            'remarks': (remarks or 'Shifta payout')[:100],
            'amount': _amount(amount),
            'currencyId': settings.EMBEDLY_CURRENCY_ID,
            'customerTransactionReference': reference,
        }
        if is_staging():
            # Required on staging, rejected in production.
            payload['stagingStatus'] = settings.EMBEDLY_STAGING_PAYOUT_STATUS
        return self._request('POST', f"{self.payout_url}/Payout/inter-bank-transfer", payload)

    def payout_status(self, transaction_ref):
        return self._request('GET', f"{self.payout_url}/Payout/status/{transaction_ref}")


def is_staging():
    return 'staging' in (settings.EMBEDLY_BASE_URL or '')


def verify_webhook_signature(raw_body, signature):
    """
    HMAC-SHA512 of the exact raw body, hex, in `x-embedly-signature`.

    Embedly's docs disagree on the key (API key vs a webhook secret), so it is
    configurable: EMBEDLY_WEBHOOK_SECRET, falling back to the API key.
    """
    secret = settings.EMBEDLY_WEBHOOK_SECRET or settings.EMBEDLY_API_KEY
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


embedly_client = EmbedlyClient()
