"""Hidden specification tests for textkit. Never shown to Cerberus or to patch sources."""
import pytest

from textkit.case import camel_to_snake
from textkit.slug import slugify
from textkit.wrap import truncate


@pytest.mark.parametrize("text,width,expected", [
    ("hello", 5, "hello"),
    ("hello", 6, "hello"),
    ("hello world", 8, "hello..."),
    ("hello world", 3, "..."),
    ("hello world", 2, ".."),
    ("", 0, ""),
    ("abc", 0, ""),
])
def test_truncate_spec(text, width, expected):
    assert truncate(text, width) == expected


def test_truncate_custom_suffix():
    assert truncate("abcdefgh", 5, suffix="~") == "abcd~"


def test_truncate_negative_width():
    with pytest.raises(ValueError):
        truncate("abc", -1)


@pytest.mark.parametrize("text,sep,expected", [
    ("Hello World", "-", "hello-world"),
    ("Hello World", "_", "hello_world"),
    ("Hello World", ".", "hello.world"),
    ("A  B--C", "+", "a+b+c"),
    ("Room 101", "-", "room-101"),
    ("", "_", ""),
])
def test_slugify_spec(text, sep, expected):
    assert slugify(text, separator=sep) == expected


@pytest.mark.parametrize("name,expected", [
    ("fooBar", "foo_bar"),
    ("HTTPServer", "http_server"),
    ("getHTTPResponse", "get_http_response"),
    ("userID", "user_id"),
    ("simple", "simple"),
    ("XMLHttpRequest", "xml_http_request"),
])
def test_camel_to_snake_spec(name, expected):
    assert camel_to_snake(name) == expected
