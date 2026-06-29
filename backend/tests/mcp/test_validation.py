from mcp_server.validation import word_count_report


def test_within_range():
    r = word_count_report("one two three four five six seven eight", 4)  # 8 words
    assert r["actual"] == 8
    assert r["target_range"] == [8, 10]
    assert r["within_range"] is True


def test_out_of_range():
    r = word_count_report("too short", 8)  # 2 words vs 18-21
    assert r["within_range"] is False


def test_unknown_duration_passes():
    r = word_count_report("anything goes here", 5)
    assert r["target_range"] is None
    assert r["within_range"] is True
