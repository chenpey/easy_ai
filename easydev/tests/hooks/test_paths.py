"""paths.patch_targets 的 unified diff 目标：单层 -p1 前缀与不可证明路径。"""

from __future__ import annotations

import sys
from pathlib import Path

HOOKS_ROOT = Path(__file__).resolve().parents[2]
if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from hooks import paths  # noqa: E402


class UnifiedPatchTargetsTests:
    def test_standard_prefixes_are_removed_once_and_deduplicated(self) -> None:
        # 预期写死：-p1 语义下 `--- a/x` 与 `+++ b/x` 是同一个真实目标。
        assert paths.patch_targets("--- a/src/x.py\n+++ b/src/x.py\n") == (["src/x.py"], True)
        # 多文件补丁按首次出现顺序去重；时间戳不是路径的一部分。
        text = (
            "diff --git a/src/x.py b/src/x.py\n"
            "--- a/src/x.py\t2026-10-29 10:00:00.000000000 +0800\n"
            "+++ b/src/x.py\t2026-10-29 10:00:01.000000000 +0800\n"
            "diff --git a/docs/g.md b/docs/g.md\n"
            "--- a/docs/g.md\n"
            "+++ b/docs/g.md\n"
        )
        assert paths.patch_targets(text) == (["src/x.py", "docs/g.md"], True)

    def test_null_rename_and_custom_prefixes(self) -> None:
        # /dev/null 那一端不是目标；空头也不制造可用伪路径。
        assert paths.patch_targets("--- /dev/null\n+++ b/src/new.py\n") == (["src/new.py"], True)
        assert paths.patch_targets("--- a/src/gone.py\n+++ /dev/null\n") == (["src/gone.py"], True)
        assert paths.patch_targets("--- /dev/null\n+++ /dev/null\n") == ([], True)
        # rename from/to 本来就是未加前缀的真实路径：原样保留，不做 a/b 剥离。
        rename = (
            "diff --git a/a/name.py b/b/name.py\n"
            "similarity index 100%\n"
            "rename from a/name.py\n"
            "rename to b/name.py\n"
        )
        assert paths.patch_targets(rename) == (["a/name.py", "b/name.py"], True)
        # a/a/x 只剥一层；custom/ 与绝对路径不剥。
        assert paths.patch_targets("--- a/a/x.py\n+++ b/a/x.py\n") == (["a/x.py"], True)
        assert paths.patch_targets("--- custom/x.py\n+++ custom/x.py\n") == (["custom/x.py"], True)
        assert paths.patch_targets("--- /abs/x.py\n+++ /abs/x.py\n") == (["/abs/x.py"], True)

    def test_quoted_and_empty_headers_are_incomplete(self) -> None:
        quoted = '--- "a/src/\\tq.py"\n+++ "b/src/\\tq.py"\n'
        assert paths.patch_targets(quoted) == ([], False)
        assert paths.patch_targets("--- \n+++ \n") == ([], False)
        # Binary files 仍是不完整补丁，已有目标保留。
        binary = "--- a/x.bin\n+++ b/x.bin\nBinary files a/x.bin and b/x.bin differ\n"
        assert paths.patch_targets(binary) == (["x.bin"], False)


class HunkBodyIsNotFileHeaderTests:
    """hunk 正文里的 `--- …`/`+++ …` 是数据：只按 `@@ -n,m +x,y @@` 计数消费正文。"""

    def test_sql_comment_body_is_not_a_target(self) -> None:
        # 被删除的 SQL 注释行 `-- a/src/app.py` 行首是 `-`，整行看起来像文件头。
        text = (
            "--- a/docs/query.sql\n"
            "+++ b/docs/query.sql\n"
            "@@ -1,2 +1 @@\n"
            "--- a/src/app.py\n"
            " SELECT 1;\n"
        )
        assert paths.patch_targets(text) == (["docs/query.sql"], True)

    def test_hunk_body_budget_ends_before_next_file_header(self) -> None:
        text = (
            "--- a/one.py\n"
            "+++ b/one.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "--- a/two.py\n"
            "+++ b/two.py\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        assert paths.patch_targets(text) == (["one.py", "two.py"], True)

    def test_short_hunk_is_incomplete(self) -> None:
        # 计数声称 5 行旧内容却只出现 1 行：目标不可证明，按不完整披露。
        text = "--- a/one.py\n+++ b/one.py\n@@ -1,5 +1 @@\n-x\n+y\n"
        targets, complete = paths.patch_targets(text)
        assert targets == ["one.py"]
        assert not complete

    def test_no_newline_marker_does_not_consume_budget(self) -> None:
        text = (
            "--- a/one.py\n"
            "+++ b/one.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "\\ No newline at end of file\n"
            "+y\n"
        )
        assert paths.patch_targets(text) == (["one.py"], True)

    def test_unicode_line_separators_stay_inside_hunk_body(self) -> None:
        """只按补丁的 LF 分行：正文里的 U+2028 不是换行，计数不能被打断。"""
        text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1 @@\n"
            "-a\u2028b\n"
            "+c\n"
        )
        assert paths.patch_targets(text) == (["src/app.py"], True)
        # 垂直制表符/U+0085 同理：它们留在正文里，不产生额外行。
        for separator in ("\u000b", "\u0085", "\u001c"):
            text = (
                "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-"
                + "x"
                + separator
                + "y"
                + "\n+c\n"
            )
            assert paths.patch_targets(text) == (["src/app.py"], True)

    def test_hunk_body_line_equal_to_three_plus_header_is_not_a_target(self) -> None:
        """正文行恰为 `+++ x`（新增一行的内容是 `++ x`）：不是文件头，只留真实目标。"""
        text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+++ x\n"
        )
        assert paths.patch_targets(text) == (["src/app.py"], True)

    def test_hunk_body_that_looks_like_added_header_is_not_a_target(self) -> None:
        text = (
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1 +1,2 @@\n"
            "-old\n"
            "+keep\n"
            "++++ b/evil.py\n"
        )
        assert paths.patch_targets(text) == (["src/app.py"], True)

    def test_new_file_hunk_counts_from_zero(self) -> None:
        text = (
            "--- /dev/null\n"
            "+++ b/src/new.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+a\n"
            "+b\n"
            "+c\n"
        )
        assert paths.patch_targets(text) == (["src/new.py"], True)

    def test_last_line_without_trailing_newline_still_counts(self) -> None:
        text = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new"
        assert paths.patch_targets(text) == (["src/app.py"], True)

    def test_crlf_diff_boundaries_are_preserved(self) -> None:
        text = (
            "--- a/src/app.py\r\n"
            "+++ b/src/app.py\r\n"
            "@@ -1 +1 @@\r\n"
            "-old\r\n"
            "+new\r\n"
        )
        assert paths.patch_targets(text) == (["src/app.py"], True)

    def test_added_line_that_looks_like_header_is_not_a_target(self) -> None:
        text = (
            "diff --git a/docs/q.md b/docs/q.md\n"
            "--- a/docs/q.md\n"
            "+++ b/docs/q.md\n"
            "@@ -1 +1,2 @@\n"
            " keep\n"
            "++++ b/evil.py\n"
        )
        assert paths.patch_targets(text) == (["docs/q.md"], True)
