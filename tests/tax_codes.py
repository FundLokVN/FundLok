"""Valid, unique Vietnamese tax codes for tests: projects need one (app/projects/borrower.py), and two projects with
the same 10 digits are the same business."""
import itertools
import random

_next = itertools.count(random.randrange(1_000_000_000, 9_000_000_000))


def fake_tax_code() -> str:
    """A 10-digit company tax code no other test in this run uses."""
    return f"{next(_next):010d}"
