"""Shared field validation used across masters and documents.

UAE Tax Registration Numbers are exactly 15 digits. TRN validation is applied wherever a
party TRN is captured (Organization, Customer, Vendor) so the rule lives in one place.
`party_warnings` produces non-blocking advisories (missing TRN / address) that documents and
master outputs can surface without rejecting the record."""

from __future__ import annotations

import re

_TRN_RE = re.compile(r"\d{15}")


class ValidationError(ValueError):
    """Domain validation error → HTTP 400."""


def validate_trn(trn: str | None, *, required: bool = False, label: str = "TRN") -> str | None:
    """Return the cleaned TRN, or raise if the format is invalid.

    Empty/None is allowed unless ``required``. A non-empty value must be exactly 15 digits.
    """
    if trn is None or str(trn).strip() == "":
        if required:
            raise ValidationError(f"{label} is required.")
        return None
    t = str(trn).strip()
    if not _TRN_RE.fullmatch(t):
        raise ValidationError(f"{label} must be a 15-digit UAE Tax Registration Number.")
    return t


def party_warnings(trn: str | None, address: str | None, *, party: str = "Party") -> list[str]:
    """Non-blocking advisories for a customer/vendor used on a tax document."""
    warnings: list[str] = []
    if not (trn and str(trn).strip()):
        warnings.append(f"{party} TRN is missing — required for a valid UAE tax invoice.")
    if not (address and str(address).strip()):
        warnings.append(f"{party} address is missing.")
    return warnings
