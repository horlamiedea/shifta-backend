from datetime import date
from decimal import Decimal

from rest_framework import serializers

from . import corporate
from .models import Transaction
from shifts.models import ShiftApplication


class TransactionProfessionalSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    email = serializers.EmailField()
    status = serializers.CharField(required=False)


class TransactionShiftSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    role = serializers.CharField()
    specialty = serializers.CharField()
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    rate = serializers.DecimalField(max_digits=12, decimal_places=2)
    facility_name = serializers.CharField(allow_null=True)


class TransactionSerializer(serializers.ModelSerializer):
    type = serializers.CharField(source='transaction_type')
    description = serializers.SerializerMethodField()
    shift = serializers.SerializerMethodField()
    professional = serializers.SerializerMethodField()
    professionals = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        fields = [
            'id', 'type', 'amount', 'status', 'created_at',
            'direction', 'balance_after', 'reference',
            'description', 'shift', 'professional', 'professionals',
        ]

    # -- description ----------------------------------------------------------

    DESCRIPTIONS = {
        'FUNDING': 'Wallet funding',
        'WITHDRAWAL': 'Wallet withdrawal',
        'REVERSAL': 'Withdrawal reversed',
        'ADJUSTMENT': 'Balance adjustment',
    }

    SHIFT_DESCRIPTIONS = {
        'CHARGE': 'Shift created — {role}',
        'PAYOUT': 'Payment for {role} shift',
        'REFUND': 'Refund — {role} shift',
    }

    def get_description(self, obj):
        if obj.transaction_type in self.DESCRIPTIONS:
            return self.DESCRIPTIONS[obj.transaction_type]
        if obj.shift and obj.transaction_type in self.SHIFT_DESCRIPTIONS:
            return self.SHIFT_DESCRIPTIONS[obj.transaction_type].format(role=obj.shift.role)
        return obj.transaction_type.capitalize()

    # -- shift ----------------------------------------------------------------

    def get_shift(self, obj):
        if not obj.shift:
            return None
        return {
            'id': str(obj.shift.id),
            'role': obj.shift.role,
            'specialty': obj.shift.specialty,
            'start_time': obj.shift.start_time,
            'end_time': obj.shift.end_time,
            'rate': str(obj.shift.rate),
            'facility_name': obj.shift.facility.name if obj.shift.facility else None,
        }

    # -- professional (single, for PAYOUT) ------------------------------------

    def get_professional(self, obj):
        if not obj.shift or obj.transaction_type != 'PAYOUT':
            return None
        app = (
            ShiftApplication.objects
            .filter(shift=obj.shift, professional__user=obj.user)
            .select_related('professional__user')
            .first()
        )
        if not app:
            return None
        return _serialize_professional(app.professional)

    # -- professionals (list, for CHARGE/REFUND on facility side) -------------

    def get_professionals(self, obj):
        request = self.context.get('request')
        if (
            not obj.shift
            or obj.transaction_type not in ('CHARGE', 'REFUND')
            or not request
            or not request.user.is_facility
        ):
            return None
        apps = (
            ShiftApplication.objects
            .filter(
                shift=obj.shift,
                status__in=['CONFIRMED', 'IN_PROGRESS', 'COMPLETED'],
            )
            .select_related('professional__user')
        )
        result = [
            {**_serialize_professional(a.professional), 'status': a.status}
            for a in apps
        ]
        return result or None


def _serialize_professional(professional):
    name = f"{professional.user.first_name} {professional.user.last_name}".strip()
    return {
        'id': str(professional.id),
        'name': name or professional.user.email,
        'email': professional.user.email,
    }


class WalletKycSerializer(serializers.Serializer):
    """Professionals send bvn or nin + dob/address/city; facilities send tin."""
    bvn = serializers.RegexField(r'^\d{11}$', required=False, allow_blank=True)
    nin = serializers.RegexField(r'^\d{11}$', required=False, allow_blank=True)
    dob = serializers.DateField(required=False)
    address = serializers.CharField(required=False, max_length=255)
    city = serializers.CharField(required=False, max_length=100)
    middle_name = serializers.CharField(required=False, allow_blank=True, max_length=100)
    tin = serializers.CharField(required=False, max_length=30)


class WithdrawalSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    bank_code = serializers.RegexField(r'^\d{3,6}$')
    account_number = serializers.RegexField(r'^\d{10}$')
    account_name = serializers.CharField(required=False, allow_blank=True)  # ignored; bank's name is used


class AmountSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))


DIRECTOR_FIELDS = (
    'director_first_name', 'director_last_name', 'director_email', 'director_phone', 'director_dob',
    'director_address', 'director_bvn', 'director_nin', 'director_meter_number', 'director_disco_code',
)


class CorporateKycSerializer(serializers.Serializer):
    """
    Facility wallet onboarding (multipart/form-data). The first submission
    needs the TIN, one director and all six documents; later submissions may
    add another director or replace the documents.
    """
    tin = serializers.CharField(required=False, max_length=30)
    city = serializers.CharField(required=False, max_length=100)

    director_first_name = serializers.CharField(required=False, max_length=100)
    director_last_name = serializers.CharField(required=False, max_length=100)
    director_middle_name = serializers.CharField(required=False, allow_blank=True, max_length=100)
    director_email = serializers.EmailField(required=False)
    director_phone = serializers.CharField(required=False, max_length=20)
    director_dob = serializers.DateField(required=False)
    director_address = serializers.CharField(required=False, max_length=255)
    director_bvn = serializers.RegexField(r'^\d{11}$', required=False)
    director_nin = serializers.RegexField(r'^\d{11}$', required=False)
    director_meter_number = serializers.CharField(required=False, max_length=30)
    director_disco_code = serializers.ChoiceField(choices=corporate.DISCO_CODES, required=False)

    # Six documents (PDF/JPEG/PNG, max 5 MB). CAC falls back to the one on the facility profile.
    cac = serializers.FileField(required=False)
    tin_document = serializers.FileField(required=False)
    board_resolution = serializers.FileField(required=False)
    utility_bill = serializers.FileField(required=False)
    memart = serializers.FileField(required=False)
    scuml = serializers.FileField(required=False)

    def validate(self, data):
        given = [f for f in DIRECTOR_FIELDS if data.get(f)]
        missing = [f for f in DIRECTOR_FIELDS if not data.get(f)]
        if given and missing:
            raise serializers.ValidationError({missing[0]: 'Required when adding a director.'})
        dob = data.get('director_dob')
        if dob:
            today = date.today()
            if today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day)) < 18:
                raise serializers.ValidationError({'director_dob': 'Director must be at least 18.'})
        return data

    def director(self):
        d = self.validated_data
        if not d.get('director_bvn'):
            return None
        return corporate.Director(
            first_name=d['director_first_name'], last_name=d['director_last_name'],
            middle_name=d.get('director_middle_name', ''), email=d['director_email'],
            phone=d['director_phone'], dob=d['director_dob'], address=d['director_address'],
            bvn=d['director_bvn'], nin=d['director_nin'], meter_number=d['director_meter_number'],
            disco_code=d['director_disco_code'],
        )

    def documents(self, fallback_cac=None):
        """Uploaded documents as corporate.Document, or None if none were sent."""
        docs = {}
        for field, _, _ in corporate.DOCUMENTS:
            upload = self.validated_data.get(field)
            if upload is not None:
                if upload.size > corporate.MAX_DOCUMENT_BYTES:
                    raise ValueError(f"{field} must be 5 MB or smaller.")
                docs[field] = corporate.make_document(upload.name, upload.read())
        if not docs:
            return None
        if 'cac' not in docs and fallback_cac:
            fallback_cac.open('rb')
            try:
                docs['cac'] = corporate.make_document(fallback_cac.name.rsplit('/', 1)[-1], fallback_cac.read())
            finally:
                fallback_cac.close()
        return docs
