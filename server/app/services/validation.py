"""Field validation shared by setup, settings and meter forms (see docs/server-ui.md)."""
import re

from email_validator import EmailNotValidError, validate_email

USERNAME_RE = re.compile(r"^[a-z0-9_.\-]{3,32}$")
PHONE_RE = re.compile(r"^\+[0-9]{8,15}$")


def username(value):
    value = (value or "").strip().lower()
    if not USERNAME_RE.match(value):
        return value, "Username must be 3–32 characters: letters, digits, _ . -"
    return value, None


def password(value, confirm=None):
    value = value or ""
    if confirm is not None and value != confirm:
        return "Passwords do not match"
    if not 10 <= len(value) <= 128:
        return "Password must be 10–128 characters"
    if not re.search(r"[a-z]", value) or not re.search(r"[A-Z]", value):
        return "Password needs upper and lower case letters"
    if not re.search(r"[0-9]", value):
        return "Password needs at least one digit"
    if not re.search(r"[^A-Za-z0-9]", value):
        return "Password needs at least one symbol"
    return None


def email(value, required=True):
    value = (value or "").strip()
    if not value:
        return value, ("Email is required" if required else None)
    if len(value) > 254:
        return value, "Email is too long"
    try:
        value = validate_email(value, check_deliverability=False).normalized
    except EmailNotValidError:
        return value, "Email is not valid"
    return value, None


def phone(value, required=True):
    value = re.sub(r"[\s\-()]", "", value or "")
    if not value:
        return value, ("Phone number is required" if required else None)
    if not PHONE_RE.match(value):
        return value, "Phone must be international format, e.g. +60123456789"
    return value, None


def text(value, label, min_len, max_len):
    value = (value or "").strip()
    if not min_len <= len(value) <= max_len:
        if min_len:
            return value, f"{label} must be {min_len}–{max_len} characters"
        return value, f"{label} must be at most {max_len} characters"
    return value, None
