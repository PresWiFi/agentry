import pytest

from agentry.textutil import clean_html, cosine, html_to_text, parse_json


def test_parse_json_variants():
    assert parse_json('{"a": 1}') == {"a": 1}
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! Here it is: {"a": {"b": "}"}} hope that helps') == {"a": {"b": "}"}}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_clean_html_strips_active_content():
    out = clean_html('<p onclick="x()">Hi</p><script>alert(1)</script><img src=x onerror=alert(1)>'
                     '<a href="javascript:alert(1)">a</a><a href="https://ok.example">b</a><iframe src="//x"></iframe>')
    assert "script" not in out and "onerror" not in out and "onclick" not in out and "javascript" not in out
    assert "iframe" not in out
    assert '<a href="https://ok.example">b</a>' in out
    assert out.startswith("<p>Hi</p>")


def test_html_to_text_and_cosine():
    text = html_to_text("<ol><li>One</li><li>Two</li></ol><script>x()</script>")
    assert "- One" in text and "- Two" in text and "x()" not in text
    assert cosine([1, 0], [1, 0]) == pytest.approx(1)
    assert cosine([1, 0], [0, 1]) == 0
