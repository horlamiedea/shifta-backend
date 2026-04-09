"""
Embedly Wallet API Client
Handles all communication with Embedly's wallet-as-a-service API.
"""
import requests
import logging
import hashlib
import hmac
import json
from django.conf import settings

logger = logging.getLogger(__name__)


class EmbedlyClient:
    def __init__(self):
        self.base_url = settings.EMBEDLY_BASE_URL.rstrip('/')
        self.payout_url = settings.EMBEDLY_PAYOUT_URL.rstrip('/')
        self.api_key = settings.EMBEDLY_API_KEY
        self.organisation_id = settings.EMBEDLY_ORGANISATION_ID
        self.country_id = settings.EMBEDLY_COUNTRY_ID
        self.currency_id = settings.EMBEDLY_CURRENCY_ID
        self.customer_type_id = settings.EMBEDLY_CUSTOMER_TYPE_ID
        self.headers = {
            'x-api-key': self.api_key,
            'Content-Type': 'application/json',
        }

    def _request(self, method, url, payload=None, timeout=30):
        try:
            response = requests.request(
                method, url, json=payload, headers=self.headers, timeout=timeout
            )
            data = response.json()
            logger.info(f"Embedly {method} {url} -> {response.status_code}: {data}")
            return {
                'success': response.status_code in (200, 201),
                'status_code': response.status_code,
                'data': data,
            }
        except requests.exceptions.RequestException as e:
            logger.error(f"Embedly request failed: {e}")
            return {'success': False, 'status_code': 0, 'data': {'error': str(e)}}

    def create_customer(self, first_name, last_name, email, phone_number):
        """Create a customer on Embedly."""
        url = f"{self.base_url}/customer"
        payload = {
            'firstName': first_name or 'User',
            'lastName': last_name or 'Shifta',
            'emailAddress': email,
            'phoneNumber': phone_number or '',
            'organisationId': self.organisation_id,
            'customerTypeId': self.customer_type_id,
        }
        return self._request('POST', url, payload)

    def create_wallet(self, customer_id):
        """Create a closed wallet for a customer."""
        url = f"{self.base_url}/wallet"
        payload = {
            'customerId': customer_id,
            'currencyId': self.currency_id,
            'countryId': self.country_id,
            'organisationId': self.organisation_id,
        }
        return self._request('POST', url, payload)

    def get_wallet(self, account_number):
        """Get wallet details including balance."""
        url = f"{self.base_url}/wallet/{account_number}"
        return self._request('GET', url)

    def wallet_to_wallet(self, source_account, destination_account, amount, reference, narration=''):
        """Transfer between wallets."""
        url = f"{self.base_url}/wallet/transfer"
        payload = {
            'sourceAccountNumber': source_account,
            'destinationAccountNumber': destination_account,
            'amount': float(amount),
            'reference': reference,
            'narration': narration or 'Shifta Transfer',
        }
        return self._request('POST', url, payload)

    def process_payout(self, source_account, destination_account_number, destination_bank_code,
                       destination_account_name, amount, reference, narration=''):
        """Process inter-bank payout (withdrawal to bank account)."""
        url = f"{self.payout_url}/v1/transfer"
        payload = {
            'sourceAccountNumber': source_account,
            'destinationAccountNumber': destination_account_number,
            'destinationBankCode': destination_bank_code,
            'destinationAccountName': destination_account_name,
            'amount': float(amount),
            'reference': reference,
            'narration': narration or 'Shifta Payout',
        }
        return self._request('POST', url, payload)

    def get_banks(self):
        """Get list of supported banks."""
        url = f"{self.payout_url}/v1/banks"
        return self._request('GET', url)

    def bank_enquiry(self, account_number, bank_code):
        """Validate a bank account."""
        url = f"{self.payout_url}/v1/bank-enquiry"
        payload = {
            'accountNumber': account_number,
            'bankCode': bank_code,
        }
        return self._request('POST', url, payload)

    @staticmethod
    def verify_signature(raw_body, signature):
        """Verify webhook signature."""
        hmac_obj = hmac.new(
            settings.EMBEDLY_API_KEY.encode('utf-8'),
            raw_body if isinstance(raw_body, bytes) else raw_body.encode('utf-8'),
            hashlib.sha512
        )
        return hmac.compare_digest(hmac_obj.hexdigest(), signature)


embedly_client = EmbedlyClient()
