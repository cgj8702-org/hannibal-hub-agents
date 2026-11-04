"""Unit tests for diff_tools utilities, including parse_patches_from_unified_diff."""

from webhook_agent.tools.diff_tools import (
    added_line_anchors,
    parse_patches_from_unified_diff,
    verify_line_reference,
    walk_right_side,
)


def test_parse_patches_from_unified_diff_empty_or_whitespace():
    assert parse_patches_from_unified_diff("") == {}
    assert parse_patches_from_unified_diff("   \n\n  ") == {}


def test_parse_patches_from_unified_diff_single_file():
    diff = """diff --git a/src/main.py b/src/main.py
index 1234567..89abcdef 100644
--- a/src/main.py
+++ b/src/main.py
@@ -10,3 +10,4 @@ def run():
     print("hello")
+    print("world")
     return 0
"""
    patches = parse_patches_from_unified_diff(diff)
    assert "src/main.py" in patches
    assert patches["src/main.py"].startswith("@@ -10,3 +10,4 @@ def run():")
    assert '+    print("world")' in patches["src/main.py"]


def test_parse_patches_from_unified_diff_multiple_files():
    diff = """diff --git a/uv.lock b/uv.lock
index 1111111..2222222 100644
--- a/uv.lock
+++ b/uv.lock
@@ -370,5 +370,5 @@
 [[package]]
 name = "authlib"
-version = "1.7.2"
+version = "1.9.1"
diff --git a/pyproject.toml b/pyproject.toml
index 3333333..4444444 100644
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -20,3 +20,3 @@
-authlib = "1.7.2"
+authlib = "1.9.1"
"""
    patches = parse_patches_from_unified_diff(diff)
    assert set(patches.keys()) == {"uv.lock", "pyproject.toml"}
    assert '+version = "1.9.1"' in patches["uv.lock"]
    assert '+authlib = "1.9.1"' in patches["pyproject.toml"]


def test_parse_patches_from_unified_diff_added_and_deleted_files():
    diff = """diff --git a/new_file.py b/new_file.py
new file mode 100644
index 0000000..1234567
--- /dev/null
+++ b/new_file.py
@@ -0,0 +1,2 @@
+line1
+line2
diff --git a/old_file.py b/old_file.py
deleted file mode 100644
index 1234567..0000000
--- a/old_file.py
+++ /dev/null
@@ -1,2 +0,0 @@
-old1
-old2
"""
    patches = parse_patches_from_unified_diff(diff)
    assert "new_file.py" in patches
    assert "+line1" in patches["new_file.py"]
    assert "old_file.py" in patches
    assert "-old1" in patches["old_file.py"]


def test_parse_patches_from_unified_diff_binary_or_no_hunk():
    diff = """diff --git a/assets/logo.png b/assets/logo.png
new file mode 100644
index 0000000..1234567
Binary files /dev/null and b/assets/logo.png differ
"""
    patches = parse_patches_from_unified_diff(diff)
    assert patches == {}


def test_walk_right_side_on_parsed_patch():
    diff = """diff --git a/uv.lock b/uv.lock
index 1111111..2222222 100644
--- a/uv.lock
+++ b/uv.lock
@@ -370,5 +370,5 @@
 [[package]]
 name = "authlib"
-version = "1.7.2"
+version = "1.9.1"
"""
    anchors, text = walk_right_side(diff)
    assert "uv.lock" in anchors
    assert 372 in anchors["uv.lock"]
    assert 372 in text["uv.lock"]
    assert 'version = "1.9.1"' in text["uv.lock"][372]
    assert verify_line_reference(diff, "uv.lock", 372) is True
    assert verify_line_reference(diff, "uv.lock", 9999) is False
