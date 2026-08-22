"""Every user-facing string in one place.

Not decoration: these strings end up in the response envelope, so a typo is a
UI bug and a reworded sentence is an API change. Keeping them together makes
both reviewable, and gives the frontend a fixed vocabulary to match against.
"""


class ErrorMessage:
    # ── auth ─────────────────────────────────────────────────────────────────
    INVALID_CREDENTIALS = "Incorrect email or password"
    COULD_NOT_VALIDATE_CREDENTIALS = "Could not validate credentials"
    NOT_ENOUGH_PERMISSIONS = "You do not have permission to do that"
    INACTIVE_USER = "This account has been deactivated"
    ACCOUNT_LOCKED = "Too many failed attempts. Try again in a few minutes"
    USER_NOT_FOUND_OR_INACTIVE = "User not found or inactive"
    EMAIL_ALREADY_EXISTS = "An account with that email already exists"
    INVALID_TOKEN_TYPE = "Wrong token type for this endpoint"
    INVALID_REFRESH_TOKEN = "Invalid or expired refresh token"
    TOKEN_REVOKED = "This token has been revoked"
    INVALID_RESET_TOKEN = "Invalid or expired reset link"
    RESET_TOKEN_USED = "This reset link has already been used"
    CURRENT_PASSWORD_INCORRECT = "Current password is incorrect"
    PASSWORD_TOO_WEAK = "Password must be at least 8 characters and contain a letter and a number"
    SAME_PASSWORD = "New password must be different from the current one"
    EMAIL_NOT_VERIFIED = "Confirm your email address before signing in"
    INVALID_VERIFY_TOKEN = "Invalid or expired confirmation link"

    # ── generic ──────────────────────────────────────────────────────────────
    RESOURCE_NOT_FOUND = "Not found"
    VALIDATION_FAILED = "Validation failed"
    INTERNAL_ERROR = "Something went wrong on our side"

    # ── projects ─────────────────────────────────────────────────────────────
    PROJECT_NOT_FOUND = "Project not found"
    PROJECT_ACCESS_DENIED = "You do not have access to this project"
    PROJECT_ROLE_TOO_LOW = "Your role on this project does not allow that"
    NOT_PROJECT_OWNER = "Only the project owner can do that"
    CANNOT_REMOVE_LAST_OWNER = "A project must keep at least one owner"
    CANNOT_CHANGE_OWN_ROLE = "You cannot change your own role on a project"
    MEMBER_ALREADY_ADDED = "That email is already on this project"
    MEMBER_NOT_FOUND = "That person is not a member of this project"
    VERSION_NOT_FOUND = "That saved version no longer exists"
    STALE_DOCUMENT = "Someone else saved a newer version of this project. Reload before saving"
    INVALID_PROJECT_DOC = "The project document is not in a shape this API understands"

    # ── admin ────────────────────────────────────────────────────────────────
    CANNOT_DEACTIVATE_SELF = "You cannot deactivate your own account"
    CANNOT_DEMOTE_LAST_SUPERADMIN = "The platform must keep at least one superadmin"
    UNKNOWN_ROLE = "Unknown role"


class ResponseMessage:
    # ── auth ─────────────────────────────────────────────────────────────────
    REGISTERED = "Account created"
    LOGIN_SUCCESS = "Signed in"
    TOKEN_REFRESHED = "Session refreshed"
    LOGGED_OUT = "Signed out"
    PASSWORD_CHANGED = "Password changed"
    PASSWORD_RESET = "Password reset"
    FORGOT_PASSWORD_SENT = "If that email has an account, a reset link is on its way"
    EMAIL_VERIFIED = "Email confirmed"
    VERIFICATION_SENT = "Confirmation email sent"

    # ── projects ─────────────────────────────────────────────────────────────
    PROJECT_CREATED = "Project created"
    PROJECT_UPDATED = "Project saved"
    PROJECT_DELETED = "Project moved to trash"
    PROJECT_RESTORED = "Project restored"
    MEMBER_ADDED = "Member added"
    MEMBER_UPDATED = "Member updated"
    MEMBER_REMOVED = "Member removed"
    LEFT_PROJECT = "You have left the project"
    VERSION_SAVED = "Version saved"
    VERSION_RESTORED = "Version restored"

    # ── admin ────────────────────────────────────────────────────────────────
    USER_CREATED = "User created"
    USER_UPDATED = "User updated"
    USER_DELETED = "User deleted"
