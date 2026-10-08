# Copyright (c) 2026 Shawn Stricker
"""Products, transports, outers and paths, and what each product is known as."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Transport(StrEnum):
    """The GATT transport a device speaks."""

    #: Service ``ff09``: ``ff09`` frames, a negotiated session.
    NEGOTIATED = "negotiated"
    #: Service ``2215``: ``ff09`` frames and negotiation on other characteristics.
    T2215 = "2215"
    #: Service ``1780``: no negotiation.
    LEGACY = "legacy"


class Outer(StrEnum):
    """How the negotiation travels."""

    #: ``4xxx`` frames under the static GCM key; the session is GCM.
    ENCRYPTED = "encrypted"
    #: ``0xxx`` frames in clear; the session is CBC.
    PLAIN = "plain"


class Path(StrEnum):
    """How the session key is established."""

    ECDH = "ecdh"
    LEGACY = "legacy"


class ModuleBuild(StrEnum):
    """The comms module's firmware build."""

    #: The Prime line's module.
    V0_2_9_7 = "v0.2.9.7"
    V0_3_0_6 = "v0.3.0.6"
    #: Refuses a cleartext connect and a non-ECDH method, and gates ``0027``.
    V0_3_3_0 = "v0.3.3.0"

    @property
    def enforces(self) -> bool:
        """Whether the build enforces the auth mode against the client."""
        return self is ModuleBuild.V0_3_3_0

    @property
    def session_ops(self) -> frozenset[int]:
        """The session requests (below ``0x40``) the module answers itself."""
        if self is ModuleBuild.V0_2_9_7:
            return PRIME_SESSION_OPS
        return C_SESSION_OPS


#: Session ops the C-series module answers on fid ``0x0f``: its inline split,
#: and the WiFi handler's ``0x22``-``0x25``.
C_SESSION_OPS = frozenset(
    {0x20, 0x22, 0x23, 0x24, 0x25, 0x27, 0x28, 0x2D, 0x2E, 0x2F, 0x30, 0x35, 0x36, 0x38}
)
#: Session ops the Prime module (v0.2.9.7) answers on fid ``0x0f``.
PRIME_SESSION_OPS = frozenset({0x20, 0x28, 0x2D, 0x2F, 0x30, 0x36})


class Product(StrEnum):
    """Anker part numbers with a known BLE client class."""

    A1722 = "A1722"
    A1728 = "A1728"
    A1753 = "A1753"
    A1754 = "A1754"
    A1755 = "A1755"
    A1761 = "A1761"
    A1763 = "A1763"
    A1765 = "A1765"
    A1780 = "A1780"
    A1781 = "A1781"
    A1783 = "A1783"
    A1785 = "A1785"
    A1790 = "A1790"
    A1790P = "A1790P"
    A17C1 = "A17C1"
    A17C5 = "A17C5"
    A2345 = "A2345"
    A2687 = "A2687"
    A91B2 = "A91B2"


@dataclass(frozen=True)
class ProductInfo:
    """What a product is called and which client classes handle it.

    Attributes:
        name: The marketing name.
        solixble_class: The SolixBLE device class.
        solix_api_category: The anker-solix-api ``SolixDeviceCategory`` value.
        transport: The GATT transport it speaks.

    """

    name: str
    solixble_class: str
    solix_api_category: str
    transport: Transport = Transport.NEGOTIATED


PRODUCTS: dict[Product, ProductInfo] = {
    Product.A1722: ProductInfo("SOLIX C300", "C300", "pps"),
    Product.A1728: ProductInfo("SOLIX C300X DC", "C300DC", "pps"),
    Product.A1753: ProductInfo("SOLIX C800", "C800", "pps"),
    Product.A1754: ProductInfo("SOLIX C800 Plus", "C800", "pps"),
    Product.A1755: ProductInfo("SOLIX C800X", "C800", "pps"),
    Product.A1761: ProductInfo("SOLIX C1000", "C1000", "pps"),
    Product.A1763: ProductInfo("SOLIX C1000 Gen 2", "C1000G2", "pps"),
    Product.A1765: ProductInfo("SOLIX C1000X Gen 2", "C1000G2", "pps"),
    Product.A1780: ProductInfo("SOLIX F2000", "F2000", "pps", Transport.LEGACY),
    Product.A1781: ProductInfo("SOLIX F2600", "F2600", "pps", Transport.LEGACY),
    Product.A1783: ProductInfo("SOLIX C2000 Gen 2", "C2000G2", "pps"),
    Product.A1785: ProductInfo("SOLIX C2000X Gen 2", "C2000G2", "pps"),
    Product.A1790: ProductInfo("SOLIX F3800", "F3800", "pps"),
    Product.A1790P: ProductInfo("SOLIX F3800 Plus", "F3800", "pps"),
    Product.A17C1: ProductInfo("Solarbank 2 E1600 Pro", "Solarbank2", "solarbank_2"),
    Product.A17C5: ProductInfo("Solarbank 3 E2700 Pro", "Solarbank3", "solarbank_3"),
    Product.A2345: ProductInfo("Prime Charger 250W", "PrimeCharger250w", "charger"),
    Product.A2687: ProductInfo("Prime Charger 160W", "PrimeCharger160w", "charger"),
    Product.A91B2: ProductInfo(
        "Prime Charging Station 240W", "PrimeChargingStation240w", "charger"
    ),
}
