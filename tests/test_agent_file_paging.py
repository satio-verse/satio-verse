"""read_file pagination: late offsets, bounded reads, and lossless long-line cursors."""
import re
import tracemalloc
import zipfile

import pytest

from wm.agent import tools as T


NEXT = re.compile(r"用 offset=(\d+), char_offset=(\d+) 接着读")


def rows(page):
    return [(int(m[1]), m[2]) for line in page.split("\n")
            if (m := re.match(r"^(\d+)\t(.*)$", line))]


def collect(path):
    """Follow only the tool's public continuation instructions, retaining long-line fragments."""
    offset, char_offset, found, cursors = 1, 0, {}, set()
    while True:
        assert (offset, char_offset) not in cursors, "continuation must make progress"
        cursors.add((offset, char_offset))
        page = T.read_file(path, offset=offset, limit=5000, char_offset=char_offset)
        assert len(page) <= T.OUT_MAX
        for number, content in rows(page):
            found[number] = found.get(number, "") + content
        if match := NEXT.search(page):
            offset, char_offset = map(int, match.groups())
        else:
            assert "还有" not in page, "large-file pages must supply an explicit cursor"
            return [found[i] for i in sorted(found)], len(cursors)


def test_small_file_keeps_existing_output(tmp_path):
    path = tmp_path / "small.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    assert T.read_file(path) == "1\tone\n2\ttwo\n3\tthree"
    assert T.read_file(path, 2, 1) == "2\ttwo\n…(还有 1 行, 用 offset 接着读)"


def test_offsets_beyond_old_byte_cap_and_real_next_line(tmp_path):
    path = tmp_path / "many-lines.txt"
    lines = [f"row {i}: " + "x" * 100 for i in range(1, 5001)]
    path.write_text("\n".join(lines), encoding="utf-8", newline="")
    assert path.stat().st_size > T.READ_MAX
    page = T.read_file(path, offset=4997, limit=2)
    assert rows(page) == [(4997, lines[4996]), (4998, lines[4997])]
    assert "第 4997–4998 行" in page
    assert NEXT.search(page).groups() == ("4999", "0")
    tail = T.read_file(path, offset=4999)
    assert rows(tail) == [(4999, lines[4998]), (5000, lines[4999])]
    assert "已到文件末尾" in tail and NEXT.search(tail) is None


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_all_pages_recover_utf8_long_lines_and_blank_lines(tmp_path, newline):
    path = tmp_path / "unicode.txt"
    # Emoji and combining characters count as Unicode code points, not UTF-8 bytes.
    lines = ["前言", "你好🙂e\u0301" * 12000, "", "第二条超长行" * 9000, "结束"]
    path.write_bytes(newline.join(lines).encode("utf-8"))
    assert path.stat().st_size > T.READ_MAX
    actual, pages = collect(path)
    assert pages > 2
    assert actual == lines


def test_long_line_in_a_small_file_also_has_lossless_cursor(tmp_path):
    path = tmp_path / "one-line.txt"
    line = "汉🙂" * 12000
    path.write_text(line, encoding="utf-8", newline="")
    assert path.stat().st_size < T.READ_MAX
    actual, pages = collect(path)
    assert actual == [line] and pages == 2


def test_char_offset_can_jump_far_into_one_line(tmp_path):
    path = tmp_path / "long-line.txt"
    line = "甲乙🙂" * 80000
    path.write_bytes((line + "\n尾行\n").encode("utf-8"))
    page = T.read_file(path, offset=1, char_offset=len(line) - 11, limit=1)
    assert rows(page) == [(1, line[-11:])]
    assert NEXT.search(page).groups() == ("2", "0")
    assert "\ufffd" not in page


@pytest.mark.parametrize("separator", ["\v", "\f", "\x85", "\u2028", "\u2029"])
def test_unicode_separators_do_not_change_line_numbers_between_paths(tmp_path, separator):
    path = tmp_path / "paragraph.txt"
    first = "a" * 25000 + separator + "still the first line"
    path.write_text(first + "\nnext\nlast", encoding="utf-8", newline="")
    assert path.stat().st_size < T.READ_MAX
    page = T.read_file(path, limit=1)
    offset, char_offset = map(int, NEXT.search(page).groups())
    rest = T.read_file(path, offset=offset, limit=1, char_offset=char_offset)
    assert rows(page)[0][1] + rows(rest)[0][1] == first
    assert NEXT.search(rest).groups() == ("2", "0")
    # A fresh line uses the small-file path again: it must keep the same numbering.
    assert T.read_file(path, offset=2, limit=1) == "2\tnext\n…(还有 1 行, 用 offset 接着读)"
    assert T.read_file(path, offset=3) == "3\tlast"


@pytest.mark.parametrize("text,offset,total", [("one", 2, 1), ("one\n", 2, 1),
                                               ("\n", 2, 1), ("one\n\ntwo", 5, 3)])
def test_past_eof_is_not_reported_as_an_empty_file(tmp_path, text, offset, total):
    path = tmp_path / "eof.txt"
    path.write_text(text, encoding="utf-8", newline="")
    page = T.read_file(path, offset=offset)
    assert "已到文件末尾" in page and f"共 {total} 行" in page
    assert "空文件" not in page and not rows(page)


def test_large_file_past_eof_counts_final_unterminated_line(tmp_path):
    path = tmp_path / "large-eof.txt"
    path.write_bytes(b"x" * (T.READ_MAX + 1) + b"\ntail")
    assert "文件共 2 行" in T.read_file(path, offset=3)
    assert "空文件" not in T.read_file(path, offset=3, char_offset=1)


def test_empty_file_blank_line_and_char_offset_at_or_past_end(tmp_path):
    path = tmp_path / "end.txt"
    path.write_bytes(b"")
    assert T.read_file(path) == "(空文件)"
    path.write_bytes(b"\n")
    assert T.read_file(path) == "1\t"
    path.write_bytes("你好🙂".encode("utf-8"))
    exact = T.read_file(path, char_offset=3)
    assert "本行共 3 个字符" in exact and "空文件" not in exact
    over = T.read_file(path, char_offset=4)
    assert "只有 3 个字符" in over and "超过行尾" in over
    path.write_bytes("你好🙂\nnext".encode("utf-8"))
    over = T.read_file(path, char_offset=4)
    assert "只有 3 个字符" in over and "offset=2, char_offset=0" in over


def test_binary_is_rejected_even_when_requesting_later_lines(tmp_path):
    path = tmp_path / "binary.bin"
    path.write_bytes(b"\x00binary\n" + b"x\n" * 120000)
    with pytest.raises(ValueError, match="二进制"):
        T.read_file(path, offset=100000)


def test_document_and_archive_suffixes_use_the_local_reader(tmp_path):
    """打不开的二进制文件先认后缀: DOCX 和 ZIP 交给本地解析器, 再按行分页."""
    document = ('<?xml version="1.0" encoding="UTF-8"?><w:document '
                'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                '<w:p><w:r><w:t>DocFileCanary76</w:t></w:r></w:p></w:body></w:document>')
    docx = tmp_path / "报告.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", document.encode("utf-8"))
    page = T.read_file(docx)
    assert "DocFileCanary76" in page and page.startswith("1\t")

    archive_path = tmp_path / "打包.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("说明.txt", "ZipFileCanary87".encode("utf-8"))
    page = T.read_file(archive_path)
    assert "ZipFileCanary87" in page


def test_unknown_suffix_with_document_header_is_read(tmp_path):
    document = ('<?xml version="1.0" encoding="UTF-8"?><w:document '
                'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                '<w:p><w:r><w:t>SniffFileCanary98</w:t></w:r></w:p></w:body></w:document>')
    path = tmp_path / "renamed.bin"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document.encode("utf-8"))
    assert "SniffFileCanary98" in T.read_file(path)


def test_renamed_archive_and_unsupported_container_explain_the_step(tmp_path):
    """后缀不对的压缩包先认文件头; 认出来但没有解压组件的, read_file 也把卡在哪一步写出来."""
    import gzip

    path = tmp_path / "mystery.bin"
    path.write_bytes(gzip.compress("RenamedGzipCanary58".encode("utf-8")))
    assert "RenamedGzipCanary58" in T.read_file(path)

    rar = tmp_path / "打包.rar"
    rar.write_bytes(b"Rar!\x1a\x07\x00" + b"\0" * 64)
    with pytest.raises(ValueError, match="RAR"):
        T.read_file(rar)

    unknown = tmp_path / "unknown.bin"
    unknown.write_bytes(b"\x00\x01\x02\x03" * 64)
    with pytest.raises(ValueError, match="二进制"):
        T.read_file(unknown)


def test_scanning_over_a_huge_line_has_bounded_memory(tmp_path, monkeypatch):
    path = tmp_path / "huge-line.txt"
    path.write_bytes(b"x" * 5_000_000 + b"\ntail\n")

    def no_whole_file_read(*args, **kwargs):
        raise AssertionError("read_file must not load the entire file before slicing")

    monkeypatch.setattr(type(path), "read_bytes", no_whole_file_read)
    tracemalloc.start()
    try:
        page = T.read_file(path, offset=2, limit=1)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert rows(page) == [(2, "tail")]
    assert peak < 1_000_000, f"scanning a 5 MB line allocated {peak} bytes"


def test_tool_schema_exposes_character_continuation():
    spec = next(s for s in T.SPECS if s["name"] == "read_file")
    props = spec["parameters"]["properties"]
    assert props["offset"]["type"] == "integer"
    assert props["char_offset"]["type"] == "integer"
    assert "Unicode" in props["char_offset"]["description"]
