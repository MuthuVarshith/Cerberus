from textkit.case import camel_to_snake
from textkit.slug import slugify
from textkit.wrap import truncate


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"


def test_slugify_accents_and_punctuation():
    assert slugify("  Crème brûlée!  ") == "creme-brulee"


def test_truncate_long_text():
    assert truncate("hello world", 8) == "hello..."


def test_truncate_short_text_unchanged():
    assert truncate("hi", 10) == "hi"


def test_camel_to_snake_simple():
    assert camel_to_snake("fooBar") == "foo_bar"


def test_camel_to_snake_trailing_acronym():
    assert camel_to_snake("userID") == "user_id"
