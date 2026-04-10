"""
Firebase Cloud Messaging push notification utility.

Setup:
1. Create a Firebase project at https://console.firebase.google.com
2. Go to Project Settings > Service Accounts > Generate New Private Key
3. Save the JSON file and set FIREBASE_CREDENTIALS_PATH in settings.py
   OR set FIREBASE_CREDENTIALS_JSON env var with the JSON content
"""
import logging
from django.conf import settings

logger = logging.getLogger(__name__)

_firebase_initialized = False


def _init_firebase():
    """Initialize Firebase Admin SDK (once)."""
    global _firebase_initialized
    if _firebase_initialized:
        return True

    try:
        import firebase_admin
        from firebase_admin import credentials

        creds_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', None)

        if creds_path:
            cred = credentials.Certificate(creds_path)
            firebase_admin.initialize_app(cred)
            _firebase_initialized = True
            return True
        else:
            logger.warning("FIREBASE_CREDENTIALS_PATH not set. Push notifications disabled.")
            return False
    except Exception as e:
        logger.error(f"Firebase init failed: {e}")
        return False


def send_push_to_user(user, title, body, data=None):
    """Send push notification to all of a user's active devices."""
    from .models import DeviceToken

    if not _init_firebase():
        return 0

    tokens = list(
        DeviceToken.objects.filter(user=user, is_active=True)
        .values_list('token', flat=True)
    )
    if not tokens:
        return 0

    return _send_to_tokens(tokens, title, body, data)


def send_push_to_users(users, title, body, data=None):
    """Send push notification to multiple users' devices."""
    from .models import DeviceToken

    if not _init_firebase():
        return 0

    tokens = list(
        DeviceToken.objects.filter(user__in=users, is_active=True)
        .values_list('token', flat=True)
    )
    if not tokens:
        return 0

    return _send_to_tokens(tokens, title, body, data)


def _send_to_tokens(tokens, title, body, data=None):
    """Send FCM message to a list of device tokens."""
    from firebase_admin import messaging
    from .models import DeviceToken

    # FCM supports max 500 tokens per multicast
    sent = 0
    failed_tokens = []

    for i in range(0, len(tokens), 500):
        batch = tokens[i:i + 500]

        message = messaging.MulticastMessage(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data={k: str(v) for k, v in (data or {}).items()},
            tokens=batch,
            android=messaging.AndroidConfig(
                priority='high',
                notification=messaging.AndroidNotification(
                    sound='default',
                    channel_id='shifta_notifications',
                ),
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound='default',
                        badge=1,
                    ),
                ),
            ),
        )

        try:
            response = messaging.send_each_for_multicast(message)
            sent += response.success_count

            # Deactivate tokens that failed with unregistered/invalid errors
            for idx, send_response in enumerate(response.responses):
                if send_response.exception:
                    exc_code = getattr(send_response.exception, 'code', '')
                    if exc_code in ('NOT_FOUND', 'UNREGISTERED', 'INVALID_ARGUMENT'):
                        failed_tokens.append(batch[idx])

        except Exception as e:
            logger.error(f"FCM send error: {e}")

    # Clean up invalid tokens
    if failed_tokens:
        DeviceToken.objects.filter(token__in=failed_tokens).update(is_active=False)
        logger.info(f"Deactivated {len(failed_tokens)} invalid FCM tokens")

    return sent
