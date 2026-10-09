"""
Client idempotency for money-moving endpoints.

Clients send `Idempotency-Key: <uuid>` on POST. The first request runs and,
if it succeeds, its response is stored; a retry with the same key gets that
stored response back (header `Idempotent-Replayed: true`) without running
again. This is what stops a double-tapped "Withdraw" button or a network
retry from moving money twice.

  - same key, different body  -> 422 (the key was reused by mistake)
  - same key while the first is still running -> 409 (retry shortly)
  - first request failed (non-2xx) -> nothing stored, the key may be retried

The header is optional so existing clients keep working; without it the
endpoint is still protected by row locks and balance checks, just not
against an intentional duplicate submission.
"""
import functools
import hashlib
import json

from django.db import IntegrityError, transaction
from rest_framework.response import Response

from .models import IdempotencyRecord

HEADER = 'Idempotency-Key'
MAX_KEY_LENGTH = 191


def _request_hash(request):
    body = json.dumps(request.data, sort_keys=True, default=str)
    return hashlib.sha256(f"{request.method}:{request.path}:{body}".encode()).hexdigest()


def idempotent(endpoint):
    """Decorate an APIView handler. `endpoint` namespaces keys per action."""

    def decorator(handler):
        @functools.wraps(handler)
        def wrapper(view, request, *args, **kwargs):
            key = request.headers.get(HEADER, '').strip()
            if not key:
                return handler(view, request, *args, **kwargs)
            if len(key) > MAX_KEY_LENGTH:
                return Response({'error': f'{HEADER} must be at most {MAX_KEY_LENGTH} characters.'}, status=400)

            request_hash = _request_hash(request)
            try:
                # Committed immediately so a concurrent duplicate sees it.
                with transaction.atomic():
                    record = IdempotencyRecord.objects.create(
                        user=request.user, endpoint=endpoint, key=key, request_hash=request_hash,
                    )
            except IntegrityError:
                record = IdempotencyRecord.objects.get(user=request.user, endpoint=endpoint, key=key)
                if record.request_hash != request_hash:
                    return Response(
                        {'error': f'{HEADER} was already used with a different request.'}, status=422,
                    )
                if record.response_status is None:
                    return Response(
                        {'error': 'A request with this Idempotency-Key is still being processed.'},
                        status=409,
                    )
                return Response(
                    record.response_body, status=record.response_status,
                    headers={'Idempotent-Replayed': 'true'},
                )

            try:
                response = handler(view, request, *args, **kwargs)
            except Exception:
                record.delete()
                raise

            if 200 <= response.status_code < 300:
                record.response_status = response.status_code
                record.response_body = response.data
                record.save(update_fields=['response_status', 'response_body', 'updated_at'])
            else:
                record.delete()
            return response

        return wrapper

    return decorator
