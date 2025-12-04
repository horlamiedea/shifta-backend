from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from core.router import route
from .services import UserRegisterService, UserLoginService, AdminVerifyFacilityService, AdminVerifyProfessionalService, ProfessionalUpdateService
from .selectors import UserSelector
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, inline_serializer
from rest_framework import serializers

@extend_schema(
    request=inline_serializer(
        name='RegisterRequest',
        fields={
            'email': serializers.EmailField(),
            'password': serializers.CharField(),
            'user_type': serializers.ChoiceField(choices=['professional', 'facility']),
            'license_number': serializers.CharField(required=False, help_text='Required for professionals'),
            'name': serializers.CharField(required=False, help_text='Required for facilities'),
            'address': serializers.CharField(required=False, help_text='Required for facilities'),
            'rc_number': serializers.CharField(required=False, help_text='Required for facilities'),
        }
    ),
    responses={
        201: inline_serializer(
            name='RegisterResponse',
            fields={
                'token': serializers.CharField(),
                'user_id': serializers.UUIDField(),
                'email': serializers.EmailField(),
                'role': serializers.CharField()
            }
        ),
        400: inline_serializer(
            name='ErrorResponse',
            fields={'error': serializers.CharField()}
        )
    }
)

@route("auth/register/", name="register")
class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")
        user_type = request.data.get("user_type") # professional or facility
        
        # Extract specific fields based on type
        extra_data = {}
        if user_type == "professional":
            extra_data["license_number"] = request.data.get("license_number")
            # Add other fields as needed
        elif user_type == "facility":
            extra_data["name"] = request.data.get("name")
            extra_data["address"] = request.data.get("address")
            extra_data["rc_number"] = request.data.get("rc_number")

        service = UserRegisterService()
        try:
            user, token = service(email=email, password=password, user_type=user_type, **extra_data)
            
            return Response({
                "token": token,
                "user_id": user.id,
                "email": user.email,
                "role": user_type
            }, status=201)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

@extend_schema(
    request=inline_serializer(
        name='LoginRequest',
        fields={
            'email': serializers.EmailField(),
            'password': serializers.CharField()
        }
    ),
    responses={
        200: inline_serializer(
            name='LoginResponse',
            fields={
                'token': serializers.CharField(),
                'user_id': serializers.UUIDField(),
                'email': serializers.EmailField()
            }
        ),
        401: inline_serializer(
            name='LoginErrorResponse',
            fields={'error': serializers.CharField()}
        )
    }
)
@route("auth/login/", name="login")
class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")
        
        service = UserLoginService()
        try:
            user, token = service(email=email, password=password)
            
            return Response({
                "token": token,
                "user_id": user.id,
                "email": user.email
            })
        except ValueError as e:
            return Response({"error": str(e)}, status=401)

@extend_schema(
    responses={
        200: inline_serializer(
            name='ProfileResponse',
            fields={
                'id': serializers.UUIDField(),
                'email': serializers.EmailField(),
                'first_name': serializers.CharField(),
                'last_name': serializers.CharField(),
                'role': serializers.CharField(),
                # Add other fields as returned by UserSelector.get_profile_data
            }
        )
    }
)
@route("auth/profile/", name="profile")
class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        selector = UserSelector()
        data = selector.get_profile_data(request.user)
        return Response(data)

    @extend_schema(
        request=inline_serializer(
            name='ProfileUpdateRequest',
            fields={
                'specialties': serializers.ListField(child=serializers.CharField(), required=False),
                'location_lat': serializers.FloatField(required=False),
                'location_lng': serializers.FloatField(required=False),
                'cv_url': serializers.URLField(required=False),
                'certificate_url': serializers.URLField(required=False),
            }
        ),
        responses={
            200: inline_serializer(
                name='ProfileUpdateResponse',
                fields={'status': serializers.CharField()}
            ),
            400: inline_serializer(
                name='ProfileUpdateErrorResponse',
                fields={'error': serializers.CharField()}
            )
        }
    )
    def put(self, request):
        user = request.user
        # Only professionals for now based on Epic 2
        if user.is_professional:
            specialties = request.data.get("specialties")
            location_lat = request.data.get("location_lat")
            location_lng = request.data.get("location_lng")
            cv_url = request.data.get("cv_url")
            certificate_url = request.data.get("certificate_url")
            
            service = ProfessionalUpdateService()
            try:
                service(user=user, specialties=specialties, location_lat=location_lat, location_lng=location_lng, cv_url=cv_url, certificate_url=certificate_url)
                return Response({"status": "updated"})
            except ValueError as e:
                return Response({"error": str(e)}, status=400)
        
        return Response({"status": "not implemented for this user type"}, status=400)


@extend_schema(
    request=inline_serializer(
        name='AdminVerifyFacilityRequest',
        fields={
            'facility_id': serializers.CharField(),
            'tier': serializers.IntegerField(),
            'credit_limit': serializers.DecimalField(max_digits=12, decimal_places=2),
        }
    ),
    responses={
        200: inline_serializer(
            name='AdminVerifyFacilityResponse',
            fields={
                'status': serializers.CharField(),
                'facility': serializers.CharField()
            }
        ),
        403: inline_serializer(name='PermissionError', fields={'error': serializers.CharField()}),
        400: inline_serializer(name='ValidationError', fields={'error': serializers.CharField()})
    }
)
@route("admin/verify-facility/", name="verify-facility")
class AdminVerifyFacilityView(APIView):
    permission_classes = [IsAuthenticated] # Should be IsAdminUser really

    def post(self, request):
        facility_id = request.data.get("facility_id")
        tier = request.data.get("tier")
        credit_limit = request.data.get("credit_limit")
        
        service = AdminVerifyFacilityService()
        try:
            facility = service(facility_id=facility_id, tier=tier, credit_limit=credit_limit, admin_user=request.user)
            return Response({"status": "verified", "facility": facility.name})
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

@extend_schema(
    request=inline_serializer(
        name='AdminVerifyProfessionalRequest',
        fields={
            'professional_id': serializers.CharField(),
        }
    ),
    responses={
        200: inline_serializer(
            name='AdminVerifyProfessionalResponse',
            fields={
                'status': serializers.CharField(),
                'professional': serializers.CharField()
            }
        ),
        403: inline_serializer(name='ProfPermissionError', fields={'error': serializers.CharField()}),
        400: inline_serializer(name='ProfValidationError', fields={'error': serializers.CharField()})
    }
)
@route("admin/verify-professional/", name="verify-professional")
class AdminVerifyProfessionalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        professional_id = request.data.get("professional_id")
        
        service = AdminVerifyProfessionalService()
        try:
            professional = service(professional_id=professional_id, admin_user=request.user)
            return Response({"status": "verified", "professional": professional.user.email})
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

@extend_schema(
    request=inline_serializer(
        name='WaitlistCreateRequest',
        fields={
            'email': serializers.EmailField(),
            'full_name': serializers.CharField(),
            'phone_number': serializers.CharField(required=False),
            'medical_type': serializers.CharField(required=False),
            'bio_data': serializers.CharField(required=False),
            'location': serializers.CharField(required=False),
            'preferred_work_address': serializers.CharField(required=False),
            'shift_rate_9hr': serializers.DecimalField(max_digits=10, decimal_places=2, required=False),
            'shift_rate_12hr': serializers.DecimalField(max_digits=10, decimal_places=2, required=False),
            'shift_rate_16hr': serializers.DecimalField(max_digits=10, decimal_places=2, required=False),
            'shift_rate_24hr': serializers.DecimalField(max_digits=10, decimal_places=2, required=False),
            'years_of_experience': serializers.IntegerField(required=False),
            'cv_file': serializers.FileField(required=False),
            'license_file': serializers.FileField(required=False),
        }
    ),
    responses={
        201: inline_serializer(
            name='WaitlistCreateResponse',
            fields={
                'status': serializers.CharField(),
                'message': serializers.CharField()
            }
        ),
        200: inline_serializer(
            name='WaitlistExistingResponse',
            fields={'message': serializers.CharField()}
        ),
        400: inline_serializer(
            name='WaitlistErrorResponse',
            fields={'error': serializers.CharField()}
        )
    }
)
@route("auth/waitlist/", name="waitlist-create")
class WaitlistCreateView(APIView):
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        from .models import WaitlistProfessional
        
        email = request.data.get("email")
        full_name = request.data.get("full_name")
        phone_number = request.data.get("phone_number")
        medical_type = request.data.get("medical_type")
        bio_data = request.data.get("bio_data")
        location = request.data.get("location")
        preferred_work_address = request.data.get("preferred_work_address")
        shift_rate_9hr = request.data.get("shift_rate_9hr")
        shift_rate_12hr = request.data.get("shift_rate_12hr")
        shift_rate_16hr = request.data.get("shift_rate_16hr")
        shift_rate_24hr = request.data.get("shift_rate_24hr")
        years_of_experience = request.data.get("years_of_experience")
        
        cv_file = request.FILES.get("cv_file")
        license_file = request.FILES.get("license_file")
        
        if not email or not full_name:
             return Response({"error": "Email and Name are required"}, status=400)
             
        if WaitlistProfessional.objects.filter(email=email).exists():
            return Response({"message": "Already on waitlist"}, status=200)
            
        WaitlistProfessional.objects.create(
            email=email,
            full_name=full_name,
            phone_number=phone_number,
            medical_type=medical_type,
            bio_data=bio_data,
            location=location,
            preferred_work_address=preferred_work_address,
            shift_rate_9hr=shift_rate_9hr,
            shift_rate_12hr=shift_rate_12hr,
            shift_rate_16hr=shift_rate_16hr,
            shift_rate_24hr=shift_rate_24hr,
            years_of_experience=years_of_experience,
            cv_file=cv_file,
            license_file=license_file
        )
        
        return Response({"status": "success", "message": "Added to waitlist"}, status=201)

@extend_schema(
    request=inline_serializer(
        name='FacilityDocumentUploadRequest',
        fields={
            'cac_file': serializers.FileField(required=False),
            'license_file': serializers.FileField(required=False),
            'other_documents': serializers.FileField(required=False),
        }
    ),
    responses={
        200: inline_serializer(
            name='FacilityDocumentUploadResponse',
            fields={
                'status': serializers.CharField(),
                'message': serializers.CharField()
            }
        ),
        403: inline_serializer(name='DocPermissionError', fields={'error': serializers.CharField()})
    }
)
@route("facility/documents/", name="facility-document-upload")
class FacilityDocumentUploadView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        if not request.user.is_facility:
            return Response({"error": "Only facilities can upload documents"}, status=403)
            
        facility = request.user.facility
        
        cac_file = request.FILES.get("cac_file")
        license_file = request.FILES.get("license_file")
        other_documents = request.FILES.get("other_documents")
        
        if cac_file:
            facility.cac_file = cac_file
        if license_file:
            facility.license_file = license_file
        if other_documents:
            facility.other_documents = other_documents
            
        facility.save()
        
        return Response({"status": "success", "message": "Documents uploaded successfully"})

@extend_schema(
    responses={
        200: inline_serializer(
            name='FacilityStaffListResponse',
            many=True,
            fields={
                'id': serializers.CharField(),
                'email': serializers.EmailField(),
                'role': serializers.CharField(),
                'permissions': inline_serializer(
                    name='StaffPermissions',
                    fields={
                        'can_create_shifts': serializers.BooleanField(),
                        'can_manage_staff': serializers.BooleanField()
                    }
                )
            }
        ),
        403: inline_serializer(name='StaffListPermissionError', fields={'error': serializers.CharField()})
    }
)
@route("facility/staff/", name="facility-staff-list")
class FacilityStaffListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.is_facility:
            return Response({"error": "Only facilities can view staff"}, status=403)
            
        facility = request.user.facility
        # Get all staff associated with this facility
        # Note: The FacilityStaff model links User to Facility. 
        # We need to fetch FacilityStaff objects where facility=facility
        from .models import FacilityStaff
        
        staff_members = FacilityStaff.objects.filter(facility=facility)
        
        data = [{
            "id": str(s.id),
            "email": s.user.email,
            "role": s.role,
            "permissions": {
                "can_create_shifts": s.can_create_shifts,
                "can_manage_staff": s.can_manage_staff
            }
        } for s in staff_members]
        
        return Response(data)

@extend_schema(
    request=inline_serializer(
        name='FacilityStaffCreateRequest',
        fields={
            'email': serializers.EmailField(),
            'password': serializers.CharField(),
            'role': serializers.ChoiceField(choices=['ADMIN', 'MANAGER', 'STAFF']),
            'permissions': inline_serializer(
                name='StaffPermissionsCreate',
                fields={
                    'can_create_shifts': serializers.BooleanField(required=False),
                    'can_manage_staff': serializers.BooleanField(required=False),
                    'can_view_financials': serializers.BooleanField(required=False)
                }
            )
        }
    ),
    responses={
        201: inline_serializer(
            name='FacilityStaffCreateResponse',
            fields={'status': serializers.CharField(), 'staff_id': serializers.CharField()}
        ),
        400: inline_serializer(name='StaffCreateError', fields={'error': serializers.CharField()}),
        403: inline_serializer(name='StaffCreatePermissionError', fields={'error': serializers.CharField()})
    }
)
@route("facility/staff/create/", name="facility-staff-create")
class FacilityStaffCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_facility:
            # Check if it's a staff member with permissions
            if hasattr(request.user, 'facility_staff_profile') and request.user.facility_staff_profile.can_manage_staff:
                facility = request.user.facility_staff_profile.facility
            else:
                return Response({"error": "Permission denied"}, status=403)
        else:
            facility = request.user.facility

        email = request.data.get("email")
        password = request.data.get("password")
        role = request.data.get("role")
        permissions = request.data.get("permissions", {})
        
        from .services import FacilityStaffService
        service = FacilityStaffService()
        try:
            staff = service.create_staff(facility, email, password, role, permissions)
            return Response({"status": "created", "staff_id": str(staff.id)}, status=201)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

@extend_schema(
    request=inline_serializer(
        name='FacilityStaffUpdateRequest',
        fields={
            'role': serializers.ChoiceField(choices=['ADMIN', 'MANAGER', 'STAFF'], required=False),
            'permissions': inline_serializer(
                name='StaffPermissionsUpdate',
                fields={
                    'can_create_shifts': serializers.BooleanField(required=False),
                    'can_manage_staff': serializers.BooleanField(required=False),
                    'can_view_financials': serializers.BooleanField(required=False)
                }
            )
        }
    ),
    responses={
        200: inline_serializer(
            name='FacilityStaffUpdateResponse',
            fields={'status': serializers.CharField()}
        ),
        400: inline_serializer(name='StaffUpdateError', fields={'error': serializers.CharField()}),
        403: inline_serializer(name='StaffUpdatePermissionError', fields={'error': serializers.CharField()})
    }
)
@route("facility/staff/<uuid:staff_id>/update/", name="facility-staff-update")
class FacilityStaffUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, staff_id):
        if not request.user.is_facility:
             if hasattr(request.user, 'facility_staff_profile') and request.user.facility_staff_profile.can_manage_staff:
                facility = request.user.facility_staff_profile.facility
             else:
                return Response({"error": "Permission denied"}, status=403)
        else:
            facility = request.user.facility
            
        from .models import FacilityStaff
        try:
            staff = FacilityStaff.objects.get(id=staff_id, facility=facility)
        except FacilityStaff.DoesNotExist:
            return Response({"error": "Staff member not found"}, status=404)
            
        role = request.data.get("role")
        permissions = request.data.get("permissions")
        
        from .services import FacilityStaffService
        service = FacilityStaffService()
        service.update_staff(staff, role, permissions)
        
        return Response({"status": "updated"})

