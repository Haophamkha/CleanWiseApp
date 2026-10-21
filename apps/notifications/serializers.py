from rest_framework import serializers

from .models import Notification, NotificationPreference


class NotificationSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source='get_type_display', read_only=True)

    class Meta:
        model = Notification
        fields = [
            'id', 'title', 'message', 'type', 'type_display',
            'related_booking', 'related_schedule', 'related_worker',
            'is_read', 'read_at', 'created_at', 'navigation_source', 'target',
        ]
        read_only_fields = fields


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = ['push_enabled']