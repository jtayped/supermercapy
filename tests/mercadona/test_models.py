from __future__ import annotations

from decimal import Decimal

import pytest

import supermercapy.mercadona as mercadona
from supermercapy import InvalidResponseError, Photo, Unit, UnitPrice
from supermercapy.mercadona import HomeNotification, MercadonaPhoto, PhotoFit
from supermercapy.mercadona.models import (
    parse_category,
    parse_home_section,
    parse_product,
    parse_product_summary,
)
from tests.conftest import read_fixture


def test_photo_url_generation_is_pure_and_validated() -> None:
    photo = MercadonaPhoto(
        file_name="https://example.test/path/image name.jpg?old=query"
    )
    assert isinstance(photo, Photo)
    assert photo.file_name == "image name.jpg"
    assert photo.url == "https://prod-mercadona.imgix.net/images/image name.jpg"
    assert photo.sized() == photo.url
    assert photo.sized(width=640) == (
        "https://prod-mercadona.imgix.net/images/image name.jpg?fit=crop&w=640"
    )
    assert photo.sized(height=480, fit=PhotoFit.FIT) == (
        "https://prod-mercadona.imgix.net/images/image name.jpg?fit=fit&h=480"
    )
    assert photo.sized(width=640, height=480) == (
        "https://prod-mercadona.imgix.net/images/image name.jpg?fit=crop&h=480&w=640"
    )
    for kwargs in ({"width": 0}, {"height": -1}, {"fit": "stretch"}):
        with pytest.raises(ValueError):
            photo.sized(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", "folder/image.jpg", "folder\\image.jpg"])
def test_photo_rejects_invalid_file_names(value: str) -> None:
    with pytest.raises(ValueError):
        MercadonaPhoto(file_name=value)


def test_optional_malformed_values_are_ignored() -> None:
    product = parse_product_summary(
        {
            "id": 7,
            "display_name": "Test",
            "limit": {},
            "published": "yes",
            "unavailable_weekdays": [1, True, "2"],
            "price_instructions": {
                "unit_price": " 1.200 ",
                "unit_size": [],
                "price_decreased": "yes",
            },
            "thumbnail": {"bad": "shape"},
            "badges": "bad",
            "categories": "bad",
        }
    )
    assert product.id == "7"
    assert product.price.amount == Decimal("1.200")
    assert product.price.unit_size is None
    assert not product.price.is_discounted
    assert product.availability.available is None
    assert product.availability.unavailable_weekdays == (1,)
    assert product.thumbnail is None
    assert product.category_ids == ()


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (parse_product_summary, {}),
        (parse_product, {"id": True, "display_name": "Bad"}),
        (parse_category, {"id": 1, "name": ""}),
    ],
)
def test_core_identity_is_required(parser: object, payload: object) -> None:
    with pytest.raises(InvalidResponseError):
        parser(payload)  # type: ignore[operator]


def test_store_package_exports_are_deliberate() -> None:
    assert "Mercadona" in mercadona.__all__
    assert "CatalogResult" in mercadona.__all__
    assert "discover_warehouses" in mercadona.__all__
    assert "parse_product" not in mercadona.__all__


def test_an_explicit_level_wins_over_the_position_in_the_tree() -> None:
    category = parse_category(
        {
            "id": 6,
            "name": "Huevos",
            "level": 0,
            "categories": [{"id": 72, "name": "x"}],
        },
        level=5,
    )

    assert category.level == 0
    assert category.children[0].level == 1
    assert parse_category({"id": 6, "name": "Huevos"}).level is None


def test_a_notification_action_may_still_be_a_bare_path() -> None:
    section = parse_home_section(
        {"layout": "notification", "content": {"title": "x", "action": "/categories"}}
    )

    notification = section.items[0]
    assert isinstance(notification, HomeNotification)
    assert notification.action == "/categories"
    assert notification.action_title is None


def test_a_notification_action_object_gives_its_url_and_its_button_title() -> None:
    section = parse_home_section(
        {
            "layout": "notification",
            "content": {
                "title": "x",
                "action": {"title": "Entrar", "redirect_url": "/login"},
            },
        }
    )

    notification = section.items[0]
    assert isinstance(notification, HomeNotification)
    assert notification.action == "/login"
    assert notification.action_title == "Entrar"


def test_a_notification_without_an_action_has_neither_url_nor_title() -> None:
    section = parse_home_section({"layout": "notification", "content": {"title": "x"}})

    notification = section.items[0]
    assert isinstance(notification, HomeNotification)
    assert notification.action is None
    assert notification.action_title is None


def test_a_photo_url_without_a_file_name_is_dropped() -> None:
    summary = parse_product_summary(
        {"id": 1, "display_name": "x", "thumbnail": "https://example.test/"}
    )

    assert summary.thumbnail is None


def test_the_reference_price_of_the_fixture_is_per_litre() -> None:
    product = parse_product(read_fixture("mercadona", "product_full.json"))

    assert product.price.unit_price == Decimal("1.2300")
    assert product.price.unit_price_unit == "L"
    assert product.price.reference == UnitPrice(amount=Decimal("1.23"), unit=Unit.LITRE)


@pytest.mark.parametrize(
    ("reference_price", "reference_format", "expected"),
    [
        # every reference_format seen on october 2026 searches
        ("0.830", "L", UnitPrice(amount=Decimal("0.83"), unit=Unit.LITRE)),
        ("1.200", "kg", UnitPrice(amount=Decimal("1.20"), unit=Unit.KILOGRAM)),
        # 24 eggs at 5.25 and 12 at 3.05: both codes are a dozen
        ("2.625", "dz", UnitPrice(amount=Decimal("0.2188"), unit=Unit.PIECE)),
        ("3.050", "dc", UnitPrice(amount=Decimal("0.2542"), unit=Unit.PIECE)),
        ("0.070", "lv", UnitPrice(amount=Decimal("0.07"), unit=Unit.DOSE)),
        ("0.384", "ud", UnitPrice(amount=Decimal("0.384"), unit=Unit.PIECE)),
        ("0.075", "m", UnitPrice(amount=Decimal("0.075"), unit=Unit.METRE)),
        ("1.000", "hj", None),
        (None, "kg", None),
        ("1.000", None, None),
    ],
)
def test_every_reference_format_is_normalised(
    reference_price: str | None,
    reference_format: str | None,
    expected: UnitPrice | None,
) -> None:
    product = parse_product_summary(
        {
            "id": "1",
            "display_name": "x",
            "price_instructions": {
                "unit_price": "1.00",
                "reference_price": reference_price,
                "reference_format": reference_format,
            },
        }
    )

    assert product.price.reference == expected
    assert product.price.unit_price_unit == reference_format
