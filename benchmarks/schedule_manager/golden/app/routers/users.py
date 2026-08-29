"""Administrator-only user management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from ..dependencies import AdminUser, UserRepositoryDependency

router = APIRouter(prefix="/users", tags=["users"])


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    _admin: AdminUser,
    users: UserRepositoryDependency,
) -> Response:
    """Delete one user and cascade their schedules (administrators only)."""
    if not users.delete(user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"user not found: {user_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
