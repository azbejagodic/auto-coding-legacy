"""Lossless oversized-hunk regressions; no provider or subprocess execution."""

from dataclasses import replace
import hashlib
import json

import pytest

from auto_coding.reviews import (
    FileChange, ReviewConfig, ReviewError, build_chunks, encoded, request_arguments, template,
)

from test_reviews import packet


def addition(path, lines):
    header = (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
              f"index 0000000..abcdef0\n--- /dev/null\n+++ b/{path}\n")
    return FileChange(path, "A", header + f"@@ -0,0 +1,{len(lines)} @@\n"
                      + "".join("+" + line for line in lines))


def large_addition(kind):
    if kind == "css":
        lines = [f".tile-{n} {{ padding: {n % 10}px; }}\n" for n in range(320)]
    else:
        lines = [f'const row{n} = {{ label: "item-{n}", active: true }};\n' for n in range(160)]
    return addition(f"board.{kind}", lines)


def sections(chunks):
    return [section for chunk in chunks for section in json.loads(chunk.content)["sections"]]


def check_fragment_coverage(change, chunks, old_start, new_start):
    """Independently reconstruct the patch and verify every primary/context range."""
    header, original = change.patch.split("@@ ", 1)
    hunk_header, body = ("@@ " + original).split("\n", 1)
    hunk_header += "\n"
    # Only LF is a Git line delimiter, including for CRLF and Unicode source.
    lines = [line + "\n" for line in body.split("\n")[:-1]]
    if not body.endswith("\n"):
        lines.append(body.split("\n")[-1])
    items = sections(chunks)
    assert len(items) > 1 and all("fragment" in item for item in items)
    assert header + hunk_header + "".join(item["body"] for item in items) == change.patch
    cursor = 0
    old_cursor, new_cursor = old_start, new_start
    for item in items:
        info = item["fragment"]
        start, end = info["primary_body_lines"]
        assert start == cursor and start < end <= len(lines)
        assert item["section_id"] == f"0:0:{start}-{end}"
        assert item["header"] == header
        assert item["path"] == change.path and item["old_path"] == change.old_path
        assert item["change_type"] == change.change_type
        assert info["parent_section_id"] == "0:0"
        assert info["hunk_header"] == hunk_header and info["hunk_body_lines"] == len(lines)
        assert item["body"] == "".join(lines[start:end])
        old_count = sum(line.startswith((" ", "-")) for line in lines[start:end])
        new_count = sum(line.startswith((" ", "+")) for line in lines[start:end])
        assert info["old_range"] == {"start": old_cursor, "count": old_count}
        assert info["new_range"] == {"start": new_cursor, "count": new_count}
        for side, anchor in (("before", start), ("after", end)):
            context = info["context_only"][side]
            begin, stop = context["body_lines"]
            assert 0 <= begin <= stop <= len(lines)
            assert (stop if side == "before" else begin) == anchor
            assert context["body"] == "".join(lines[begin:stop])
        # A marker is never separated from its preceding payload line.
        assert not lines[start].startswith("\\")
        if end < len(lines):
            assert not lines[end].startswith("\\")
        old_cursor += old_count
        new_cursor += new_count
        cursor = end
    assert cursor == len(lines)
    coverage = [identity for chunk in chunks for identity in chunk.coverage]
    assert coverage == [item["section_id"] for item in items]
    assert len(set(coverage)) == len(coverage)


@pytest.mark.parametrize("kind", ["css", "js"])
def test_large_new_file_complete_deterministic_bounded_coverage(packet, kind):
    change = large_addition(kind)
    packet = replace(packet, changes=(change,))
    config = ReviewConfig(request_budget=12000)  # Same batch limits; no increases.
    chunks = build_chunks(packet, config)
    check_fragment_coverage(change, chunks, 0, 1)
    assert chunks == build_chunks(packet, config)
    assert sum(chunk.estimated_tokens for chunk in chunks) <= config.max_review_tokens
    for index, chunk in enumerate(chunks, 1):
        content = json.loads(chunk.content)
        assert content["chunk_index"] == index and content["chunk_count"] == len(chunks)
        assert content["partial_diff"] is True
        assert content["requirements"] == packet.requirements
        assert content["acceptance_criteria"] == list(packet.acceptance_criteria)
        assert content["tests"] == packet.tests
        assert content["changed_files"] == [
            {"path": change.path, "old_path": None, "change_type": "A"}]
        wire = request_arguments(config, template(), chunk.content)
        assert wire["reasoning_effort"] == "medium"
        assert chunk.estimated_tokens == (
            len(encoded(wire).encode("utf-8")) + config.output_tokens + config.headroom)
        assert chunk.estimated_tokens <= config.request_budget
        assert chunk.chunk_id == hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("kind", ["M", "D", "R"])
def test_oversized_hunk_preserves_line_references_and_special_source(packet, kind):
    header = ("diff --git a/old.js b/board.js\nindex abc..def 100644\n"
              "--- a/old.js\n+++ b/board.js\n")
    # Replacement pairs, real context, CRLF, Unicode separators, bare CR,
    # quotes/backslashes and the exact Git no-final-newline marker.
    count = 150 if kind == "D" else 70
    old_lines = [f"-old line {n}: \u2028\rtext " + "x" * 20 + "\r\n" for n in range(count)]
    if kind == "D":
        hunk_header = f"@@ -37,{count} +0,0 @@\n"
        body = "".join(old_lines) + "\\ No newline at end of file\n"
        old_start, new_start = 37, 0
    else:
        hunk_header = f"@@ -37,{count + 2} +73,{count + 2} @@ function board()\n"
        body = " shared heading\r\n" + "".join(
            old + f'+new line {n}: "\\\\label" ' + "y" * 20 + "\r\n"
            for n, old in enumerate(old_lines)
        ) + " shared ending\r\n\\ No newline at end of file\n"
        old_start, new_start = 37, 73
    change = FileChange("board.js", kind, header + hunk_header + body,
                        "old.js" if kind == "R" else None)
    chunks = build_chunks(replace(packet, changes=(change,)), ReviewConfig(request_budget=12000))
    check_fragment_coverage(change, chunks, old_start, new_start)


def test_impossible_essential_evidence_still_refused(packet):
    packet = replace(packet, requirements="Complete approved requirements.\n" * 1000)
    with pytest.raises(ReviewError, match="Essential review evidence"):
        build_chunks(packet, ReviewConfig(request_budget=12000))


def test_repeated_essentials_can_leave_too_little_room_for_a_line(packet):
    config = ReviewConfig(request_budget=12000)
    packet = replace(packet, requirements="Approved scope. " * 300)
    # The requirements alone fit, but an indivisible source line cannot fit
    # alongside them. Never omit requirements to get a successful review.
    tiny = build_chunks(packet, config)
    assert len(tiny) == 1 and tiny[0].estimated_tokens <= config.request_budget
    packet = replace(packet, changes=(addition("board.js", ["x" * 4000 + "\n"]),))
    with pytest.raises(ReviewError, match="diff line or required hunk context"):
        build_chunks(packet, config)


def test_single_unbroken_line_is_not_truncated(packet):
    change = addition("minified.js", ["x" * 20000 + "\n"])
    with pytest.raises(ReviewError, match="diff line or required hunk context"):
        build_chunks(replace(packet, changes=(change,)), ReviewConfig(request_budget=12000))


@pytest.mark.parametrize("config,message", [
    (ReviewConfig(request_budget=12000, max_chunks=1), "chunks"),
    (ReviewConfig(request_budget=12000, max_review_tokens=12000), "Total review allowance"),
])
def test_fragmentation_preserves_global_limits(packet, config, message):
    with pytest.raises(ReviewError, match=message):
        build_chunks(replace(packet, changes=(large_addition("css"),)), config)


def test_split_hunk_does_not_resegment_other_files_or_hunks(packet):
    small = packet.changes[0]
    large = large_addition("css")
    chunks = build_chunks(replace(packet, changes=(small, large)),
                          ReviewConfig(request_budget=12000))
    items = sections(chunks)
    assert items[0]["section_id"] == "0:0" and "fragment" not in items[0]
    assert items[0]["header"] + items[0]["body"] == small.patch
    assert all(item["fragment"]["parent_section_id"] == "1:0" for item in items[1:])


def test_623_insertions_across_six_files_are_fully_covered_offline(packet):
    # Synthetic regression with the reported commit shape, not its private data.
    changes = (addition("large.css", [f".t{n % 10}{{gap:0}}\n" for n in range(550)]),) + tuple(
        addition(f"part{n}.js", [f"let a{i}=0;\n" for i in range(count)])
        for n, count in enumerate((15, 15, 15, 14, 14))
    )
    configuration = {"model": "codex-test", "permitted_paths": [c.path for c in changes],
                     "test_commands": [["python", "-m", "pytest"]]}
    packet = replace(packet, changes=changes, plan_fingerprint="f" * 64,
                     approved_configuration=configuration)
    chunks = build_chunks(packet, ReviewConfig(request_budget=12000))
    assert len(chunks) > 1
    for change in changes:
        items = [item for item in sections(chunks) if item["path"] == change.path]
        original_header, original_body = change.patch.split("@@ ", 1)
        if "fragment" in items[0]:
            reconstructed = (items[0]["fragment"]["hunk_header"]
                             + "".join(item["body"] for item in items))
        else:
            assert len(items) == 1
            reconstructed = items[0]["body"]
        assert original_header + reconstructed == change.patch
        assert reconstructed == "@@ " + original_body
    for chunk in chunks:
        assert json.loads(chunk.content)["approved_configuration"] == configuration
        assert json.loads(chunk.content)["plan_fingerprint"] == packet.plan_fingerprint
        assert chunk.estimated_tokens <= 12000
    assert sum(chunk.estimated_tokens for chunk in chunks) <= 30000


@pytest.mark.parametrize("marker", [
    "\\ No newline at end of file\n-old\n+new\n",
    "-old\n\\ unexpected marker\n+new\n",
    "-old\n\\ No newline at end of file\n\\ No newline at end of file\n+new\n",
])
def test_malformed_markers_remain_fail_closed(packet, marker):
    change = replace(packet.changes[0], patch=packet.changes[0].patch.split("@@")[0]
                     + "@@ -1 +1 @@\n" + marker)
    with pytest.raises(ReviewError, match="hunk body"):
        build_chunks(replace(packet, changes=(change,)), ReviewConfig())
