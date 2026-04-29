"""Unit tests for unified diff helper validation."""
from __future__ import annotations

from darkfactory.tools.patch_helpers import apply_unified_diff


VALID_DIFF = """\
diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1,2 @@
 hello
+world
"""


def test_valid_diff_passes():
    assert apply_unified_diff(VALID_DIFF) is True


def test_mangled_diff_rejected_no_headers():
    assert apply_unified_diff("not a diff at all\njust words\n") is False


def test_diff_without_hunk_rejected():
    no_hunk = """\
diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
"""
    assert apply_unified_diff(no_hunk) is False


def test_empty_diff_rejected():
    assert apply_unified_diff("") is False
