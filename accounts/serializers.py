from django.contrib.auth import password_validation
from rest_framework import serializers

from .models import User


class UserSummarySerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='display_name', read_only=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'name', 'role']


class CurrentUserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='display_name', read_only=True)
    is_administrator = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'name', 'role', 'is_administrator']


class UserAdminSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, style={'input_type': 'password'})

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'role', 'is_active', 'password', 'date_joined', 'last_login']
        read_only_fields = ['date_joined', 'last_login']

    def validate_password(self, value):
        password_validation.validate_password(value, self.instance)
        return value

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        if not password:
            raise serializers.ValidationError({'password': 'A password is required for new users.'})
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(style={'input_type': 'password'})
