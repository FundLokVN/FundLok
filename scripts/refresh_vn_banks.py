"""Regenerate app/direct_transfer/data/vn_banks.json from VietQR's public bank list, cross-checked against SePay's.

    python -I scripts/refresh_vn_banks.py            # writes the file
    python -I scripts/refresh_vn_banks.py --check    # exits 1 if the committed file is out of date

A BIN that is wrong sends a payer's money to the wrong bank, so this refuses to write if the two sources disagree
on the BIN of any bank code they share, or if any BIN is not 6 digits. Review the diff before committing.
Downloaded data is treated as untrusted: it is parsed as JSON only.
"""
import datetime as dt
import json
import re
import ssl
import sys
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "app" / "direct_transfer" / "data" / "vn_banks.json"
VIETQR = "https://api.vietqr.io/v2/banks"
SEPAY = "https://qr.sepay.vn/banks.json"
WALLETS = {"momo", "ZALOPAY", "MVAS"}  # not banks: off by default (banks.py)
POPULAR = ["VCB", "ICB", "BIDV", "VBA", "TCB", "MB", "ACB", "VPB", "TPB", "STB", "HDB", "VIB"]


def _ssl_context() -> ssl.SSLContext:
    """python.org builds of Python on macOS ship without a CA store; certifi (already a project dependency) fixes
    that. Certificate checking is never turned off."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "fundlok-refresh-vn-banks/1.0 (+bank list refresh)"})
    with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as resp:  # noqa: S310 (fixed https URLs above)
        return json.loads(resp.read().decode("utf-8"))


def build() -> dict:
    vietqr = fetch(VIETQR)["data"]
    sepay = fetch(SEPAY)["data"]
    problems = [b["code"] for b in vietqr if not re.fullmatch(r"\d{6}", str(b["bin"]))]
    sepay_bins = {b["code"]: b["bin"] for b in sepay}
    mismatches = [(b["code"], b["bin"], sepay_bins[b["code"]]) for b in vietqr
                  if b["code"] in sepay_bins and sepay_bins[b["code"]] != b["bin"]]
    if problems or mismatches:
        sys.exit(f"Refusing to write. Non-6-digit BINs: {problems}. BIN mismatches (code, vietqr, sepay): {mismatches}")
    shared = sum(1 for b in vietqr if b["code"] in sepay_bins)
    banks = [
        {"bin": b["bin"], "code": b["code"], "short_name": b["shortName"].strip(), "name": b["name"].strip(),
         "kind": "WALLET" if b["code"] in WALLETS else "BANK", "popular": b["code"] in POPULAR}
        for b in vietqr if b["transferSupported"] == 1
    ]
    banks.sort(key=lambda x: (not x["popular"], POPULAR.index(x["code"]) if x["popular"] else 0, x["short_name"].lower()))
    return {
        "_comment": "Vietnamese banks and e-wallets that can receive a VietQR transfer, keyed by NAPAS BIN. "
                    "Regenerate with scripts/refresh_vn_banks.py; do not hand-edit.",
        "source": f"{VIETQR} (transferSupported = 1)",
        "retrieved": dt.date.today().isoformat(),
        "cross_checked_against": f"{SEPAY}: 0 BIN mismatches across {shared} shared bank codes",
        "banks": banks,
    }


if __name__ == "__main__":
    new = build()
    if "--check" in sys.argv:
        old = json.loads(OUT.read_text(encoding="utf-8"))
        sys.exit(0 if old["banks"] == new["banks"] else 1)
    OUT.write_text(json.dumps(new, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {len(new['banks'])} entries to {OUT}")
