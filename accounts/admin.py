from django.contrib import admin
from .models import User, Professional, Facility

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('email', 'is_staff', 'is_active', 'date_joined')
    search_fields = ('email',)
    list_filter = ('is_staff', 'is_active')

@admin.register(Professional)
class ProfessionalAdmin(admin.ModelAdmin):
    list_display = ('user', 'license_number', 'is_verified', 'created_at')
    search_fields = ('user__email', 'license_number')
    list_filter = ('is_verified',)

from django.contrib import admin
from django.shortcuts import render, redirect
from django.urls import path
from django import forms
from django.contrib import messages
from .models import User, Professional, Facility
from billing.models import AdminWalletLog

class FundFacilityForm(forms.Form):
    amount = forms.DecimalField(max_digits=12, decimal_places=2)
    comment = forms.CharField(widget=forms.Textarea)

@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'wallet_balance', 'credit_limit', 'is_verified', 'created_at')
    search_fields = ('name', 'user__email')
    list_filter = ('is_verified', 'tier')
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
        facility = Facility.objects.get(id=facility_id)
        if request.method == 'POST':
            form = FundFacilityForm(request.POST)
            if form.is_valid():
                amount = form.cleaned_data['amount']
                comment = form.cleaned_data['comment']
                
                # Update Balance
                facility.wallet_balance += amount
                facility.save()
                
                # Log
                AdminWalletLog.objects.create(
                    admin_user=request.user,
                    facility=facility,
                    amount=amount,
                    comment=comment
                )
                
                self.message_user(request, f"Successfully funded {facility.name} with {amount}")
                return redirect('admin:accounts_facility_changelist')
        else:
            form = FundFacilityForm()
            
        context = {
            'title': f'Fund {facility.name}',
            'form': form,
            'opts': self.model._meta,
        }
        return render(request, 'admin/fund_facility_form.html', context)
