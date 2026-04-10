from rest_framework import serializers
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
            'description', 'shift', 'professional', 'professionals',
        ]

    # -- description ----------------------------------------------------------

    DESCRIPTIONS = {
        'FUNDING': 'Wallet funding',
        'WITHDRAWAL': 'Wallet withdrawal',
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
