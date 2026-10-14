from django.contrib import admin
from .models import User, Professional, Facility, WaitlistProfessional, WaitlistFacility

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('email', 'is_staff', 'is_active', 'date_joined')
    search_fields = ('email',)
    list_filter = ('is_staff', 'is_active')

@admin.register(Professional)
class ProfessionalAdmin(admin.ModelAdmin):
    list_display = ('user', 'license_number', 'wallet_balance', 'is_verified', 'created_at')
    search_fields = ('user__email', 'license_number')
    list_filter = ('is_verified',)
    # Balances change only through billing.ledger, never by direct edit.
    readonly_fields = ('wallet_balance',)

import uuid
from decimal import Decimal

from django.shortcuts import render, redirect
from django.urls import path
from django import forms
from django.contrib import messages
from django.db import transaction
from billing import ledger
from billing.models import AdminWalletLog

class FundFacilityForm(forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    # Money may only enter against a payment actually received (billing.accounting).
    payment_reference = forms.CharField(
        max_length=120,
        help_text="Reference of the payment received for this amount, e.g. the bank transfer session ID.",
    )
    comment = forms.CharField(widget=forms.Textarea)
    # Issued when the form renders; a double-submit reuses it and is ignored.
    idempotency_key = forms.CharField(widget=forms.HiddenInput)

@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'wallet_balance', 'credit_limit', 'is_verified', 'created_at')
    search_fields = ('name', 'user__email')
    list_filter = ('is_verified', 'tier')
    # Balances change only through billing.ledger (use the Fund action).
    readonly_fields = ('wallet_balance',)
    actions = ['fund_facility']

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('fund-facility/<uuid:facility_id>/', self.admin_site.admin_view(self.fund_facility_view), name='fund-facility'),
        ]
        return custom_urls + urls

    def fund_facility(self, request, queryset):
        if 'apply' in request.POST:
            # This part is tricky with intermediate page for multiple items.
            # For simplicity, let's assume single selection or handle one by one.
            # But standard action just receives a queryset.
            # Let's redirect to the first selected facility's funding page.
            if queryset.count() != 1:
                self.message_user(request, "Please select exactly one facility to fund.", level=messages.WARNING)
                return
            
            facility = queryset.first()
            return redirect('admin:fund-facility', facility_id=facility.id)
            
        return render(request, 'admin/fund_facility_intermediate.html', context={'queryset': queryset})
    
    fund_facility.short_description = "Fund Facility Wallet"

    def fund_facility_view(self, request, facility_id):
        if not request.user.is_superuser:
            self.message_user(request, "Only superusers can fund a facility by hand.", level=messages.ERROR)
            return redirect('admin:accounts_facility_changelist')
        facility = Facility.objects.get(id=facility_id)
        if request.method == 'POST':
            form = FundFacilityForm(request.POST)
            if form.is_valid():
                amount = form.cleaned_data['amount']
                comment = form.cleaned_data['comment']
                payment_reference = form.cleaned_data['payment_reference'].strip()

                with transaction.atomic():
                    _, created = ledger.credit(
                        facility.user, amount, 'FUNDING',
                        f"admin-fund:{form.cleaned_data['idempotency_key']}",
                        metadata={'admin': request.user.email, 'comment': comment,
                                  'payment_reference': payment_reference},
                    )
                    if created:
                        AdminWalletLog.objects.create(
                            admin_user=request.user,
                            facility=facility,
                            amount=amount,
                            comment=comment
                        )

                if created:
                    self.message_user(request, f"Successfully funded {facility.name} with {amount}")
                else:
                    self.message_user(request, "This funding was already applied.", level=messages.WARNING)
                return redirect('admin:accounts_facility_changelist')
        else:
            form = FundFacilityForm(initial={'idempotency_key': uuid.uuid4().hex})
            
        context = {
            'title': f'Fund {facility.name}',
            'form': form,
            'opts': self.model._meta,
        }
        return render(request, 'admin/fund_facility_form.html', context)


@admin.register(WaitlistProfessional)
class WaitlistProfessionalAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'email', 'medical_type', 'location', 'created_at')
    search_fields = ('full_name', 'email', 'phone_number', 'location')
    list_filter = ('medical_type', 'created_at')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(WaitlistFacility)
class WaitlistFacilityAdmin(admin.ModelAdmin):
    list_display = (
        'facility_name',
        'contact_name',
        'email',
        'facility_type',
        'staffing_need',
        'created_at',
    )
    search_fields = ('facility_name', 'contact_name', 'email', 'phone_number', 'location')
    list_filter = ('facility_type', 'staffing_need', 'created_at')
    readonly_fields = ('created_at', 'updated_at')
