from django.contrib import admin
from .models import Project

@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        'project_code',
        'name',
        'status',
        'manager',
        'budget'
    )

    search_fields = (
        'project_code',
        'name'
    )

    list_filter = (
        'status',
        'is_active'
    )