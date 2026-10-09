# Copyright (c) 2026 Shawn Stricker
"""Profiles generated from anker-solix-api maps and SolixBLE classes.

Written by ``tools/generate_profiles.py``. A product leaves here when it
gets a recorded profile of its own.
"""

from anker_ble_emulator.products import Product

from .a17c5 import PROFILE as A17C5
from .a1723 import PROFILE as A1723
from .a1726 import PROFILE as A1726
from .a1728 import PROFILE as A1728
from .a1753 import PROFILE as A1753
from .a1754 import PROFILE as A1754
from .a1755 import PROFILE as A1755
from .a1781 import PROFILE as A1781
from .a1790 import PROFILE as A1790
from .a1790p import PROFILE as A1790P


#: The generated profiles, by product.
MAP_BUILT = {
    Product.A1723: A1723,
    Product.A1726: A1726,
    Product.A1728: A1728,
    Product.A1753: A1753,
    Product.A1754: A1754,
    Product.A1755: A1755,
    Product.A1781: A1781,
    Product.A1790: A1790,
    Product.A1790P: A1790P,
    Product.A17C5: A17C5,
}
