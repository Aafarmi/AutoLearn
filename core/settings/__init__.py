"""Local settings contracts for model profiles and safe UI integration."""

from .model_profiles import (
    ModelProfile,
    ModelProfileError,
    ModelProfileStore,
    ProfileUpdateConfirmationRequired,
    profile_changed_after,
    require_profile_confirmation,
)

__all__ = [
    "ModelProfile",
    "ModelProfileError",
    "ModelProfileStore",
    "ProfileUpdateConfirmationRequired",
    "profile_changed_after",
    "require_profile_confirmation",
]
