import pytest

from src.calculator import add, average, divide, multiply, percentage, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2


def test_multiply():
    assert multiply(4, 3) == 12


def test_divide():
    assert divide(10, 2) == 5


def test_divide_by_zero_returns_none():
    assert divide(1, 0) is None


def test_divide_by_zero_negative_numerator():
    assert divide(-5, 0) is None


def test_average():
    assert average([1, 2, 3]) == 2


def test_average_empty():
    assert average([]) is None


def test_percentage():
    assert percentage(25, 200) == pytest.approx(12.5)


def test_percentage_of_zero_returns_none():
    assert percentage(5, 0) is None
