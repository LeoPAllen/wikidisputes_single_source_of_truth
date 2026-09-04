"""Independent revision-to-revision comment reconstruction (Method B).

Method B is deliberately isolated from the existing full-page matcher (Method A).
Installing this package does not alter any selected annotation representation.
"""

from .safety import METHOD_B_SAFETY_VERSION, MethodBSafetyDecision, assess_method_b_safety

METHOD_B_METHOD = "mediawiki_revision_diff"
METHOD_B_VERSION = "1.0.0"

__all__ = [
    "METHOD_B_METHOD",
    "METHOD_B_SAFETY_VERSION",
    "METHOD_B_VERSION",
    "MethodBSafetyDecision",
    "assess_method_b_safety",
]
