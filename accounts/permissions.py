from rest_framework.permissions import SAFE_METHODS, BasePermission


def is_admin(user) -> bool:
    return bool(user and user.is_authenticated and getattr(user, 'is_administrator', False))


class IsAdministrator(BasePermission):
    message = 'Only administrators can do this.'

    def has_permission(self, request, view):
        return is_admin(request.user)


class IsAdministratorOrReadOnly(BasePermission):
    message = 'Only administrators can change this.'

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.method in SAFE_METHODS or is_admin(request.user)
