#!/usr/bin/env python3
"""
KSeF Invoice Converter - Production Edition
Converts Document-Invoice XML files to Polish KSeF FA(3) standard.

Usage:
    python3 invoice_converter.py input.xml output.xml
    python3 invoice_converter.py input.xml output.xml --config ksef_config.json
    python3 invoice_converter.py --batch input_dir/ output_dir/ --config ksef_config.json
    python3 invoice_converter.py --init-config
    python3 invoice_converter.py --validate output_ksef.xml
"""

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KSEF_NAMESPACE = "http://crd.gov.pl/wzor/2025/06/25/13775/"
SCHEMA_VERSION = "1-0E"
FORM_CODE = "FA"
FORM_VARIANT = "3"
SYSTEM_CODE = "FA (3)"

VAT_RATE_MAP = {
    "high": Decimal("23"),
    "low": Decimal("8"),
    "reduced": Decimal("5"),
    "zero": Decimal("0"),
    "none": None,
    "exempt": None,
    "zw": None,
}

KSEF_VAT_FIELDS = {
    Decimal("23"): ("P_13_1", "P_14_1"),
    Decimal("8"):  ("P_13_2", "P_14_2"),
    Decimal("5"):  ("P_13_3", "P_14_3"),
    Decimal("0"):  ("P_13_6_1", None),
    None:          ("P_13_7", None),
}

PAYMENT_TYPE_MAP = {
    "cash": "1", "gotowka": "1",
    "card": "2", "karta": "2",
    "voucher": "3", "bon": "3",
    "cheque": "4", "check": "4", "czek": "4",
    "credit": "5", "kredyt": "5",
    "transfer": "6", "bank_transfer": "6", "bank": "6", "przelew": "6",
    "mobile": "7", "blik": "7",
}

SPLIT_PAYMENT_THRESHOLD = Decimal("15000")

# EDI DocumentFunctionCode → KSeF RodzajFaktury mapping
DOC_FUNCTION_MAP = {
    "O": "VAT",      # Original
    "R": "VAT",      # Duplicate / reprint
    "C": "KOR",      # Correction
    "D": "KOR",      # Debit note (correction)
    "31": "KOR",     # Credit note / correction (UN/EDIFACT)
    "383": "KOR",    # Credit note (UN/EDIFACT)
    "380": "VAT",    # Commercial invoice (UN/EDIFACT)
    "386": "ZAL",    # Prepayment / advance
}

# EDI TaxCategoryCode values that indicate VAT exemption
EXEMPT_TAX_CATEGORIES = {"E", "AE"}

logger = logging.getLogger("ksef_converter")

# ---------------------------------------------------------------------------
# Default configuration template
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "seller_override": {
        "nip": "",
        "nazwa": "",
        "adres_l1": "",
        "adres_l2": "",
        "kod_kraju": "",
        "email": "",
        "telefon": ""
    },
    "defaults": {
        "kod_waluty": "PLN",
        "miejsce_wystawienia": "",
        "payment_days": 14,
        "forma_platnosci": "6",
        "system_info": "KSeFConverter v1.0"
    },
    "bank": {
        "nr_rb": "",
        "nazwa_banku": "",
        "opis": ""
    },
    "adnotacje": {
        "p_16": 2,
        "p_17": 2,
        "p_18": 2,
        "p_18a": 2,
        "p_23": 2
    },
    "vat_rate_overrides": {},
    "dodatkowy_opis": []
}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConversionError(Exception):
    """Raised when invoice conversion fails due to data issues."""
    pass


class ValidationError(Exception):
    """Raised when output XML fails validation checks."""
    pass


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def money(value):
    """Convert a value to Decimal rounded to 2 decimal places."""
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as e:
        raise ConversionError(f"Invalid monetary value: {value!r}") from e


def detect_encoding(file_path):
    """Detect file encoding from BOM, XML declaration, or heuristic."""
    with open(file_path, "rb") as f:
        raw = f.read(512)

    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be"

    try:
        header = raw.decode("ascii", errors="ignore")
        if "encoding=" in header:
            start = header.index("encoding=") + 9
            quote = header[start]
            if quote not in ('"', "'"):
                raise ValueError("Unquoted encoding attribute")
            end = header.index(quote, start + 1)
            declared = header[start + 1 : end].lower()
            alias = {
                "windows-1250": "cp1250",
                "windows-1252": "cp1252",
                "iso-8859-1": "latin-1",
                "iso-8859-2": "iso-8859-2",
            }
            return alias.get(declared, declared)
    except (ValueError, IndexError):
        pass

    for enc in ("utf-8", "cp1250", "iso-8859-2", "cp1252"):
        try:
            with open(file_path, "r", encoding=enc) as f:
                f.read()
            return enc
        except (UnicodeDecodeError, UnicodeError):
            continue

    return "utf-8"


def extract_nip(tax_id):
    """Extract clean NIP from a tax ID string (strip country prefix and dashes)."""
    if not tax_id:
        return None
    tax_id = tax_id.strip()
    if len(tax_id) > 2 and tax_id[:2].isalpha():
        tax_id = tax_id[2:]
    return tax_id.replace("-", "")


def extract_country(tax_id):
    """Extract ISO country code from a tax ID prefix."""
    if tax_id and len(tax_id) > 2 and tax_id[:2].isalpha():
        return tax_id[:2].upper()
    return "PL"


def get_text(element, tag, default=""):
    """Safely get trimmed text content of a child element."""
    if element is None:
        return default
    child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return default


def get_attr(element, attr, default=""):
    """Safely get trimmed attribute value from an element."""
    if element is None:
        return default
    val = element.get(attr)
    return val.strip() if val else default


def resolve_vat_rate(rate_str, overrides=None):
    """Resolve a VAT rate string to a Decimal (or None for exempt).

    Accepts named rates ('high', 'low', etc.) and numeric strings ('23', '8').
    """
    if overrides and rate_str in overrides:
        val = overrides[rate_str]
        return None if val is None else Decimal(str(val))

    key = rate_str.lower().strip() if rate_str else "high"

    if key in VAT_RATE_MAP:
        return VAT_RATE_MAP[key]

    try:
        return Decimal(key)
    except InvalidOperation:
        logger.warning("Unknown VAT rate '%s', defaulting to 23%%", rate_str)
        return Decimal("23")


def resolve_payment_type(pay_str):
    """Convert a payment type string to KSeF FormaPlatnosci code (1-7)."""
    if not pay_str:
        return "6"
    lower = pay_str.lower().strip()
    if lower in PAYMENT_TYPE_MAP:
        return PAYMENT_TYPE_MAP[lower]
    if lower.isdigit() and 1 <= int(lower) <= 7:
        return lower
    logger.warning("Unknown payment type '%s', defaulting to bank transfer (6)", pay_str)
    return "6"


def get_ksef_vat_fields(rate):
    """Return (P_13_x, P_14_x) field names for a VAT rate."""
    if rate in KSEF_VAT_FIELDS:
        return KSEF_VAT_FIELDS[rate]
    logger.warning("Non-standard VAT rate %s%%, mapping to P_13_4/P_14_4", rate)
    return ("P_13_4", "P_14_4")


def normalize_vat_rate(rate_str, overrides=None, tax_category_code=None):
    """Normalize a VAT rate string to a Decimal matching KSEF_VAT_FIELDS keys.

    '23.00' -> Decimal('23'), '8.00' -> Decimal('8'), '0.00' -> Decimal('0').
    Named rates ('high', 'exempt') are resolved via resolve_vat_rate().
    If tax_category_code indicates exemption (E, AE), returns None regardless of rate.
    """
    if tax_category_code and tax_category_code.upper() in EXEMPT_TAX_CATEGORIES:
        return None
    if not rate_str:
        return Decimal("23")
    try:
        rate = Decimal(rate_str)
        if rate == rate.to_integral_value():
            return Decimal(str(int(rate)))
        return rate
    except InvalidOperation:
        return resolve_vat_rate(rate_str, overrides)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_input_xml(file_path):
    """Parse a Document-Invoice XML file and return normalized invoice data."""
    encoding = detect_encoding(file_path)
    logger.info("Detected encoding: %s for %s", encoding, file_path)

    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        content = f.read()
    # Strip BOM remnant and XML declaration to avoid double-decoding by the parser
    content = content.lstrip("\uFEFF")
    content = content.lstrip()
    if content.startswith("<?xml"):
        pos = content.find("?>")
        if pos != -1:
            content = content[pos + 2:].lstrip()
        else:
            raise ConversionError("Malformed XML declaration: missing '?>' terminator")
    root = ET.fromstring(content)

    if root.tag != "Document-Invoice":
        raise ConversionError(
            f"Unsupported root element <{root.tag}>. Expected <Document-Invoice>."
        )

    header = root.find("Invoice-Header")
    parties = root.find("Invoice-Parties")
    lines_el = root.find("Invoice-Lines")
    summary_el = root.find("Invoice-Summary")

    if header is None:
        raise ConversionError("Missing <Invoice-Header>")
    if parties is None:
        raise ConversionError("Missing <Invoice-Parties>")
    if lines_el is None:
        raise ConversionError("Missing <Invoice-Lines>")

    # --- Header ---
    doc_number = get_text(header, "InvoiceNumber")
    doc_date = get_text(header, "InvoiceDate")
    sales_date = get_text(header, "SalesDate") or doc_date
    currency = get_text(header, "InvoiceCurrency") or "PLN"
    payment_due_date = get_text(header, "InvoicePaymentDueDate")
    doc_function_code = get_text(header, "DocumentFunctionCode")

    # Order info
    order_el = header.find("Order")
    order_number = get_text(order_el, "BuyerOrderNumber") if order_el is not None else ""
    order_date = get_text(order_el, "BuyerOrderDate") if order_el is not None else ""

    # Reference (e.g. for corrective invoices)
    ref_el = header.find("Reference")
    invoice_ref_number = get_text(ref_el, "InvoiceReferenceNumber") if ref_el is not None else ""
    invoice_ref_ksef_number = get_text(ref_el, "InvoiceReferenceKSeFNumber") if ref_el is not None else ""
    invoice_ref_date = get_text(ref_el, "InvoiceReferenceDate") if ref_el is not None else ""

    # Delivery info
    delivery_el = header.find("Delivery")
    delivery = None
    if delivery_el is not None:
        delivery = {
            "location_number": get_text(delivery_el, "DeliveryLocationNumber"),
            "date": get_text(delivery_el, "DeliveryDate"),
            "despatch_number": get_text(delivery_el, "DespatchNumber"),
            "name": get_text(delivery_el, "Name"),
            "street": get_text(delivery_el, "StreetAndNumber"),
            "city": get_text(delivery_el, "CityName"),
            "postal_code": get_text(delivery_el, "PostalCode"),
            "country": get_text(delivery_el, "Country"),
        }

    if not doc_number:
        raise ConversionError("InvoiceNumber is required")
    if not doc_date:
        raise ConversionError("InvoiceDate is required")

    # --- Parties ---
    seller_el = parties.find("Seller")
    buyer_el = parties.find("Buyer")
    payee_el = parties.find("Payee")

    if seller_el is None:
        raise ConversionError("Missing <Seller> in Invoice-Parties")
    if buyer_el is None:
        raise ConversionError("Missing <Buyer> in Invoice-Parties")

    def parse_party(el):
        return {
            "iln": get_text(el, "ILN"),
            "tax_id": get_text(el, "TaxID"),
            "name": get_text(el, "Name"),
            "street": get_text(el, "StreetAndNumber"),
            "city": get_text(el, "CityName"),
            "postal_code": get_text(el, "PostalCode"),
            "country": get_text(el, "Country"),
        }

    seller = parse_party(seller_el)
    buyer = parse_party(buyer_el)
    bank_account = get_text(payee_el, "AccountNumber") if payee_el is not None else ""

    # --- Line Items ---
    items = []
    for line in lines_el.findall("Line"):
        li = line.find("Line-Item")
        if li is None:
            continue
        items.append({
            "line_number": get_text(li, "LineNumber"),
            "ean": get_text(li, "EAN"),
            "item_code": get_text(li, "ItemCode"),
            "description": get_text(li, "ItemDescription"),
            "quantity": get_text(li, "InvoiceQuantity"),
            "unit": get_text(li, "UnitOfMeasure") or "szt",
            "unit_price": get_text(li, "InvoiceUnitNetPrice"),
            "tax_rate": get_text(li, "TaxRate"),
            "tax_category_code": get_text(li, "TaxCategoryCode"),
            "tax_amount": get_text(li, "TaxAmount"),
            "net_amount": get_text(li, "NetAmount"),
        })

    if not items:
        raise ConversionError("No line items found in <Invoice-Lines>")

    for idx, item in enumerate(items, 1):
        desc = item.get("description", "?") or "?"
        if not item.get("net_amount"):
            raise ConversionError(
                f"Missing NetAmount on line item {idx}: '{desc}'"
            )
        if not item.get("quantity"):
            raise ConversionError(
                f"Missing InvoiceQuantity on line item {idx}: '{desc}'"
            )
        if not item.get("unit_price"):
            raise ConversionError(
                f"Missing InvoiceUnitNetPrice on line item {idx}: '{desc}'"
            )
        if not item.get("tax_amount"):
            raise ConversionError(
                f"Missing TaxAmount on line item {idx}: '{desc}'"
            )

    # --- Summary ---
    sum_data = None
    if summary_el is not None:
        tax_lines = []
        ts = summary_el.find("Tax-Summary")
        if ts is not None:
            for tsl in ts.findall("Tax-Summary-Line"):
                rate = get_text(tsl, "TaxRate")
                tax_amt = get_text(tsl, "TaxAmount")
                taxable_amt = get_text(tsl, "TaxableAmount")
                tcc = get_text(tsl, "TaxCategoryCode")
                if not tax_amt or not taxable_amt:
                    raise ConversionError(
                        f"Incomplete Tax-Summary-Line (rate={rate!r}): "
                        f"TaxAmount={tax_amt!r}, TaxableAmount={taxable_amt!r}"
                    )
                tax_lines.append({
                    "rate": rate,
                    "tax_category_code": tcc,
                    "tax_amount": tax_amt,
                    "taxable_amount": taxable_amt,
                })
        sum_data = {
            "total_net": get_text(summary_el, "TotalNetAmount"),
            "total_tax": get_text(summary_el, "TotalTaxAmount"),
            "total_gross": get_text(summary_el, "TotalGrossAmount"),
            "tax_lines": tax_lines,
        }

    return {
        "doc_number": doc_number,
        "doc_date": doc_date,
        "sales_date": sales_date,
        "currency": currency,
        "payment_due_date": payment_due_date,
        "doc_function_code": doc_function_code,
        "order_number": order_number,
        "order_date": order_date,
        "invoice_ref_number": invoice_ref_number,
        "invoice_ref_ksef_number": invoice_ref_ksef_number,
        "invoice_ref_date": invoice_ref_date,
        "delivery": delivery,
        "seller": seller,
        "buyer": buyer,
        "bank_account": bank_account,
        "items": items,
        "summary": sum_data,
    }


# ---------------------------------------------------------------------------
# KSeF XML builder
# ---------------------------------------------------------------------------


def build_ksef_xml(parsed, config):
    """Build a complete KSeF FA(3) XML tree from parsed invoice data and config."""
    cfg_seller = config.get("seller_override", {})
    cfg_defaults = config.get("defaults", {})
    cfg_bank = config.get("bank", {})
    cfg_adnotacje = config.get("adnotacje", {})
    cfg_vat = {k: v for k, v in config.get("vat_rate_overrides", {}).items()
               if not k.startswith("_")}

    doc_number = parsed["doc_number"]
    doc_date = parsed["doc_date"]
    sales_date = parsed["sales_date"]
    currency = parsed.get("currency") or cfg_defaults.get("kod_waluty", "PLN")
    seller = parsed["seller"]
    buyer = parsed["buyer"]
    items = parsed["items"]
    src_summary = parsed.get("summary")
    bank_account = parsed.get("bank_account", "")
    payment_due_date = parsed.get("payment_due_date", "")
    order_number = parsed.get("order_number", "")
    order_date = parsed.get("order_date", "")
    delivery = parsed.get("delivery")
    doc_function_code = parsed.get("doc_function_code", "")
    invoice_ref_number = parsed.get("invoice_ref_number", "")
    invoice_ref_ksef_number = parsed.get("invoice_ref_ksef_number", "")
    invoice_ref_date = parsed.get("invoice_ref_date", "")
    rodzaj_faktury = DOC_FUNCTION_MAP.get(doc_function_code, "VAT")

    if rodzaj_faktury in ("KOR", "KOR_ZAL", "KOR_ROZ") and not invoice_ref_number:
        raise ConversionError(
            f"Correction invoice (RodzajFaktury={rodzaj_faktury}) requires "
            f"InvoiceReferenceNumber in <Reference> but none was found"
        )

    # --- Root ---
    ET.register_namespace("", KSEF_NAMESPACE)
    faktura = ET.Element(f"{{{KSEF_NAMESPACE}}}Faktura")

    # --- Naglowek ---
    naglowek = ET.SubElement(faktura, "Naglowek")
    kf = ET.SubElement(
        naglowek, "KodFormularza",
        kodSystemowy=SYSTEM_CODE, wersjaSchemy=SCHEMA_VERSION,
    )
    kf.text = FORM_CODE
    ET.SubElement(naglowek, "WariantFormularza").text = FORM_VARIANT
    ET.SubElement(naglowek, "DataWytworzeniaFa").text = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    ET.SubElement(naglowek, "SystemInfo").text = cfg_defaults.get(
        "system_info", "KSeFConverter v1.0"
    )

    # --- Podmiot1 (Seller) ---
    podmiot1 = ET.SubElement(faktura, "Podmiot1")
    dane1 = ET.SubElement(podmiot1, "DaneIdentyfikacyjne")

    seller_nip = cfg_seller.get("nip") or extract_nip(seller["tax_id"])
    seller_name = cfg_seller.get("nazwa") or seller["name"]
    if not seller_nip:
        raise ConversionError(
            "Seller NIP is required — set it in config or ensure input has <TaxID>"
        )

    ET.SubElement(dane1, "NIP").text = seller_nip
    ET.SubElement(dane1, "Nazwa").text = seller_name

    adres1 = ET.SubElement(podmiot1, "Adres")
    seller_country = cfg_seller.get("kod_kraju") or ""
    if not seller_country:
        seller_country = (
            extract_country(seller["tax_id"])
            if seller["tax_id"] and seller["tax_id"][:2].isalpha()
            else seller.get("country") or "PL"
        )
    ET.SubElement(adres1, "KodKraju").text = seller_country

    a1_l1 = cfg_seller.get("adres_l1") or seller.get("street", "")
    a1_l2 = cfg_seller.get("adres_l2")
    if not a1_l2:
        psc = seller.get("postal_code", "")
        city = seller.get("city", "")
        if psc or city:
            a1_l2 = f"{psc} {city}".strip()
    if a1_l1:
        ET.SubElement(adres1, "AdresL1").text = a1_l1
    if a1_l2:
        ET.SubElement(adres1, "AdresL2").text = a1_l2

    seller_email = cfg_seller.get("email")
    seller_phone = cfg_seller.get("telefon")
    if seller_email or seller_phone:
        kontakt1 = ET.SubElement(podmiot1, "DaneKontaktowe")
        if seller_email:
            ET.SubElement(kontakt1, "Email").text = seller_email
        if seller_phone:
            ET.SubElement(kontakt1, "Telefon").text = seller_phone

    # --- Podmiot2 (Buyer) ---
    podmiot2 = ET.SubElement(faktura, "Podmiot2")
    dane2 = ET.SubElement(podmiot2, "DaneIdentyfikacyjne")

    buyer_nip = extract_nip(buyer["tax_id"])
    buyer_country = (
        extract_country(buyer["tax_id"])
        if buyer["tax_id"] and buyer["tax_id"][:2].isalpha()
        else buyer.get("country") or "PL"
    )
    buyer_name = buyer["name"]
    if not buyer_nip:
        raise ConversionError("Buyer NIP / tax ID is required in input <TaxID>")

    if buyer_country == "PL":
        ET.SubElement(dane2, "NIP").text = buyer_nip
    else:
        ET.SubElement(dane2, "KodUE").text = buyer_country
        ET.SubElement(dane2, "NrVatUE").text = buyer_nip

    ET.SubElement(dane2, "Nazwa").text = buyer_name.upper()

    adres2 = ET.SubElement(podmiot2, "Adres")
    ET.SubElement(adres2, "KodKraju").text = buyer_country
    buyer_street = buyer.get("street", "")
    buyer_psc = buyer.get("postal_code", "")
    buyer_city = buyer.get("city", "")
    if buyer_street:
        ET.SubElement(adres2, "AdresL1").text = buyer_street
    if buyer_psc or buyer_city:
        ET.SubElement(adres2, "AdresL2").text = f"{buyer_psc} {buyer_city}".strip()

    ET.SubElement(podmiot2, "JST").text = "2"
    ET.SubElement(podmiot2, "GV").text = "2"

    # --- Fa ---
    fa = ET.SubElement(faktura, "Fa")
    ET.SubElement(fa, "KodWaluty").text = currency
    ET.SubElement(fa, "P_1").text = doc_date

    place = cfg_defaults.get("miejsce_wystawienia")
    if place:
        ET.SubElement(fa, "P_1M").text = place

    ET.SubElement(fa, "P_2").text = doc_number
    ET.SubElement(fa, "P_6").text = sales_date

    # --- VAT totals from Tax-Summary or calculated from line items ---
    total_net = Decimal("0")
    total_vat = Decimal("0")
    has_exempt = False

    # Build rate→tax_category_code map from summary for consistent bucketing
    summary_tcc_map = {}
    if src_summary and src_summary.get("tax_lines"):
        for tl in src_summary["tax_lines"]:
            raw_rate = tl.get("rate", "")
            tcc = tl.get("tax_category_code", "")
            if raw_rate and tcc:
                summary_tcc_map[raw_rate] = tcc

    def resolve_tcc(item_or_tl):
        """Return tax_category_code: prefer own, fall back to summary map."""
        tcc = item_or_tl.get("tax_category_code", "")
        if not tcc:
            tcc = summary_tcc_map.get(item_or_tl.get("rate") or item_or_tl.get("tax_rate", ""), "")
        return tcc

    # Collect per-rate buckets, then merge by KSeF field pair to avoid duplicates
    rate_buckets = {}

    if src_summary and src_summary.get("tax_lines"):
        for tl in src_summary["tax_lines"]:
            rate = normalize_vat_rate(
                tl["rate"], cfg_vat, resolve_tcc(tl)
            )
            net = money(tl["taxable_amount"])
            vat = money(tl["tax_amount"])
            if rate is None:
                has_exempt = True
            bucket = rate_buckets.setdefault(
                rate, {"net": Decimal("0"), "vat": Decimal("0")}
            )
            bucket["net"] += net
            bucket["vat"] += vat
    else:
        for item in items:
            rate = normalize_vat_rate(
                item["tax_rate"], cfg_vat, resolve_tcc(item)
            )
            net = money(item["net_amount"])
            vat = money(item["tax_amount"])
            if rate is None:
                has_exempt = True
            bucket = rate_buckets.setdefault(
                rate, {"net": Decimal("0"), "vat": Decimal("0")}
            )
            bucket["net"] += net
            bucket["vat"] += vat

    # Merge by KSeF field pair to prevent duplicate P_13_x/P_14_x elements
    field_buckets = {}
    for rate, bucket in rate_buckets.items():
        p13, p14 = get_ksef_vat_fields(rate)
        fb = field_buckets.setdefault(
            (p13, p14), {"net": Decimal("0"), "vat": Decimal("0")}
        )
        fb["net"] += bucket["net"]
        fb["vat"] += bucket["vat"]

    for (p13, p14) in sorted(field_buckets):
        fb = field_buckets[(p13, p14)]
        net = money(fb["net"])
        vat = money(fb["vat"])
        total_net += net
        total_vat += vat
        ET.SubElement(fa, p13).text = str(net)
        if p14 and vat > 0:
            ET.SubElement(fa, p14).text = str(vat)

    total_gross = money(total_net + total_vat)
    ET.SubElement(fa, "P_15").text = str(total_gross)

    if src_summary and src_summary.get("total_gross"):
        expected = money(src_summary["total_gross"])
        if expected != total_gross:
            raise ConversionError(
                f"Gross total mismatch: calculated {total_gross} vs "
                f"source summary {expected}"
            )

    # Per-rate consistency check: verify line-item sums match Tax-Summary
    if src_summary and src_summary.get("tax_lines"):
        line_rate_totals = {}
        for item in items:
            rate = normalize_vat_rate(
                item["tax_rate"], cfg_vat, resolve_tcc(item)
            )
            bucket = line_rate_totals.setdefault(rate, Decimal("0"))
            line_rate_totals[rate] += money(item["net_amount"])
        for tl in src_summary["tax_lines"]:
            rate = normalize_vat_rate(
                tl["rate"], cfg_vat, resolve_tcc(tl)
            )
            summary_net = money(tl["taxable_amount"])
            line_net = line_rate_totals.get(rate, Decimal("0"))
            if summary_net != line_net:
                logger.warning(
                    "Rate %s%%: Tax-Summary net %s != sum of line items net %s",
                    rate, summary_net, line_net,
                )

    # --- Adnotacje ---
    adnotacje = ET.SubElement(fa, "Adnotacje")
    ET.SubElement(adnotacje, "P_16").text = str(cfg_adnotacje.get("p_16", 2))
    ET.SubElement(adnotacje, "P_17").text = str(cfg_adnotacje.get("p_17", 2))
    ET.SubElement(adnotacje, "P_18").text = str(cfg_adnotacje.get("p_18", 2))

    p_18a = cfg_adnotacje.get("p_18a", 2)
    if currency == "PLN" and total_gross > SPLIT_PAYMENT_THRESHOLD:
        p_18a = 1
        logger.info(
            "Split payment auto-enabled (gross %s PLN > %s PLN threshold)",
            total_gross, SPLIT_PAYMENT_THRESHOLD,
        )
    ET.SubElement(adnotacje, "P_18A").text = str(p_18a)

    zwolnienie = ET.SubElement(adnotacje, "Zwolnienie")
    if has_exempt:
        p19 = ET.SubElement(zwolnienie, "P_19")
        p19_basis = cfg_adnotacje.get(
            "p_19_basis", "Zwolnione z VAT na podstawie art. 43 ust. 1 ustawy o VAT"
        )
        ET.SubElement(p19, "P_19A").text = p19_basis
    else:
        ET.SubElement(zwolnienie, "P_19N").text = "1"

    nowe_srodki = ET.SubElement(adnotacje, "NoweSrodkiTransportu")
    ET.SubElement(nowe_srodki, "P_22N").text = "1"
    ET.SubElement(adnotacje, "P_23").text = str(cfg_adnotacje.get("p_23", 2))
    pmarzy = ET.SubElement(adnotacje, "PMarzy")
    ET.SubElement(pmarzy, "P_PMarzyN").text = "1"

    # --- RodzajFaktury ---
    ET.SubElement(fa, "RodzajFaktury").text = rodzaj_faktury

    # --- Correction reference (for KOR invoices) ---
    if rodzaj_faktury in ("KOR", "KOR_ZAL", "KOR_ROZ") and invoice_ref_number:
        dane_kor = ET.SubElement(fa, "DaneFaKorygowanej")
        ET.SubElement(dane_kor, "DataWystFaKorygowanej").text = (
            invoice_ref_date or doc_date
        )
        ET.SubElement(dane_kor, "NrFaKorygowanej").text = invoice_ref_number
        if invoice_ref_ksef_number:
            ET.SubElement(dane_kor, "NrKSeF").text = "1"
            ET.SubElement(dane_kor, "NrKSeFFaKorygowanej").text = invoice_ref_ksef_number
        else:
            ET.SubElement(dane_kor, "NrKSeFN").text = "1"

    # --- DodatkowyOpis ---
    for opis in config.get("dodatkowy_opis", []):
        do = ET.SubElement(fa, "DodatkowyOpis")
        ET.SubElement(do, "Klucz").text = opis.get("klucz", "Notatka")
        ET.SubElement(do, "Wartosc").text = opis.get("wartosc", "")

    # Order number/date as DodatkowyOpis (also emitted in WarunkiTransakcji below)
    if order_number:
        do = ET.SubElement(fa, "DodatkowyOpis")
        ET.SubElement(do, "Klucz").text = "NrZamowienia"
        ET.SubElement(do, "Wartosc").text = order_number
    if order_date:
        do = ET.SubElement(fa, "DodatkowyOpis")
        ET.SubElement(do, "Klucz").text = "DataZamowienia"
        ET.SubElement(do, "Wartosc").text = order_date

    # Buyer GLN (ILN) as DodatkowyOpis
    buyer_iln = buyer.get("iln", "")
    if buyer_iln:
        do = ET.SubElement(fa, "DodatkowyOpis")
        ET.SubElement(do, "Klucz").text = "GLN_Nabywcy"
        ET.SubElement(do, "Wartosc").text = buyer_iln

    # Seller GLN (ILN) as DodatkowyOpis
    seller_iln = seller.get("iln", "")
    if seller_iln:
        do = ET.SubElement(fa, "DodatkowyOpis")
        ET.SubElement(do, "Klucz").text = "GLN_Sprzedawcy"
        ET.SubElement(do, "Wartosc").text = seller_iln

    # Delivery info as DodatkowyOpis
    if delivery:
        dlv_name = delivery.get("name", "")
        if dlv_name:
            do = ET.SubElement(fa, "DodatkowyOpis")
            ET.SubElement(do, "Klucz").text = "MiejsceDostawy"
            dlv_parts = [dlv_name]
            dlv_street = delivery.get("street", "")
            dlv_psc = delivery.get("postal_code", "")
            dlv_city = delivery.get("city", "")
            if dlv_street:
                dlv_parts.append(dlv_street)
            if dlv_psc or dlv_city:
                dlv_parts.append(f"{dlv_psc} {dlv_city}".strip())
            ET.SubElement(do, "Wartosc").text = ", ".join(dlv_parts)
        dlv_despatch = delivery.get("despatch_number", "")
        if dlv_despatch:
            do = ET.SubElement(fa, "DodatkowyOpis")
            ET.SubElement(do, "Klucz").text = "NrWZ"
            ET.SubElement(do, "Wartosc").text = dlv_despatch

    # --- FaWiersz ---
    delivery_date = delivery.get("date") if delivery else ""
    for idx, item in enumerate(items, 1):
        fw = ET.SubElement(fa, "FaWiersz")
        ET.SubElement(fw, "NrWierszaFa").text = str(idx)
        ET.SubElement(fw, "UU_ID").text = str(uuid.uuid4()).replace("-", "")[:32]
        ET.SubElement(fw, "P_6A").text = delivery_date or sales_date
        ET.SubElement(fw, "P_7").text = item["description"]
        item_code = item.get("item_code", "")
        if item_code:
            ET.SubElement(fw, "Indeks").text = item_code
        ean = item.get("ean", "")
        if ean:
            ET.SubElement(fw, "GTIN").text = ean
        ET.SubElement(fw, "P_8A").text = item.get("unit", "szt")
        ET.SubElement(fw, "P_8B").text = item["quantity"]
        ET.SubElement(fw, "P_9A").text = str(money(item["unit_price"]))
        ET.SubElement(fw, "P_11").text = str(money(item["net_amount"]))
        rate = normalize_vat_rate(
            item["tax_rate"], cfg_vat, resolve_tcc(item)
        )
        if rate is not None:
            rate_display = str(int(rate)) if rate == int(rate) else str(rate)
        else:
            rate_display = "zw"
        ET.SubElement(fw, "P_12").text = rate_display

    # --- Platnosc ---
    platnosc = ET.SubElement(fa, "Platnosc")

    if payment_due_date:
        try:
            datetime.strptime(payment_due_date, "%Y-%m-%d")
        except ValueError:
            raise ConversionError(
                f"Invalid InvoicePaymentDueDate format: {payment_due_date!r} "
                f"(expected YYYY-MM-DD)"
            )
        termin = ET.SubElement(platnosc, "TerminPlatnosci")
        ET.SubElement(termin, "Termin").text = payment_due_date
    else:
        payment_days = int(cfg_defaults.get("payment_days", 14))
        try:
            pay_date = datetime.strptime(doc_date, "%Y-%m-%d") + timedelta(
                days=payment_days
            )
        except ValueError as e:
            raise ConversionError(f"Invalid date format: {doc_date}") from e
        termin = ET.SubElement(platnosc, "TerminPlatnosci")
        ET.SubElement(termin, "Termin").text = pay_date.strftime("%Y-%m-%d")

    ET.SubElement(platnosc, "FormaPlatnosci").text = resolve_payment_type(
        cfg_defaults.get("forma_platnosci", "6")
    )

    bank_nr = cfg_bank.get("nr_rb") or bank_account
    bank_name = cfg_bank.get("nazwa_banku", "")
    if bank_nr:
        rb = ET.SubElement(platnosc, "RachunekBankowy")
        ET.SubElement(rb, "NrRB").text = bank_nr
        if bank_name:
            ET.SubElement(rb, "NazwaBanku").text = bank_name
        bank_desc = cfg_bank.get("opis", "")
        if bank_desc:
            ET.SubElement(rb, "OpisRachunku").text = bank_desc

    # --- WarunkiTransakcji (order info in schema-defined location) ---
    if order_number or order_date:
        warunki = ET.SubElement(fa, "WarunkiTransakcji")
        zamowienie = ET.SubElement(warunki, "Zamowienia")
        if order_date:
            ET.SubElement(zamowienie, "DataZamowienia").text = order_date
        if order_number:
            ET.SubElement(zamowienie, "NrZamowienia").text = order_number

    result = {
        "total_net": total_net,
        "total_vat": total_vat,
        "total_gross": total_gross,
        "line_count": len(items),
        "currency": currency,
    }
    return faktura, result


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

REQUIRED_ELEMENTS = [
    "Naglowek/KodFormularza",
    "Naglowek/WariantFormularza",
    "Naglowek/DataWytworzeniaFa",
    "Podmiot1/DaneIdentyfikacyjne/NIP",
    "Podmiot1/DaneIdentyfikacyjne/Nazwa",
    "Podmiot1/Adres/KodKraju",
    "Podmiot2/DaneIdentyfikacyjne/Nazwa",
    "Podmiot2/Adres/KodKraju",
    "Fa/KodWaluty",
    "Fa/P_1",
    "Fa/P_2",
    "Fa/P_6",
    "Fa/P_15",
    "Fa/Adnotacje/P_16",
    "Fa/Adnotacje/P_17",
    "Fa/Adnotacje/P_18",
    "Fa/Adnotacje/P_18A",
    "Fa/RodzajFaktury",
]


def validate_ksef_xml(file_path):
    """Validate a KSeF XML file for required elements and data integrity."""
    errors = []
    warnings = []

    try:
        tree = ET.parse(file_path)
    except ET.ParseError as e:
        return [f"XML parse error: {e}"], []

    root = tree.getroot()
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    for path in REQUIRED_ELEMENTS:
        ns_path = "/".join(f"{ns}{p}" for p in path.split("/"))
        elem = root.find(ns_path)
        if elem is None or not (elem.text and elem.text.strip()):
            errors.append(f"Missing or empty required element: {path}")

    fa = root.find(f"{ns}Fa")
    if fa is not None:
        line_items = fa.findall(f"{ns}FaWiersz")
        if not line_items:
            errors.append("No FaWiersz (line items) found")

        for idx, fw in enumerate(line_items, 1):
            for field in ("NrWierszaFa", "P_7", "P_8B", "P_9A", "P_11", "P_12"):
                el = fw.find(f"{ns}{field}")
                if el is None or not (el.text and el.text.strip()):
                    errors.append(f"FaWiersz[{idx}]: missing {field}")

        p15 = fa.find(f"{ns}P_15")
        if p15 is not None and p15.text:
            try:
                gross = Decimal(p15.text)
                total_net = Decimal("0")
                total_vat = Decimal("0")
                for suffix, label in [
                    ("1", "23%"), ("2", "8%"), ("3", "5%"),
                    ("4", "special"), ("6_1", "0%"), ("7", "exempt"),
                ]:
                    p13 = fa.find(f"{ns}P_13_{suffix}")
                    p14 = fa.find(f"{ns}P_14_{suffix}")
                    if p13 is not None and p13.text:
                        total_net += Decimal(p13.text)
                    if p14 is not None and p14.text:
                        total_vat += Decimal(p14.text)
                expected = money(total_net + total_vat)
                if expected != gross:
                    errors.append(
                        f"P_15 total ({gross}) != sum of P_13+P_14 ({expected})"
                    )
            except (InvalidOperation, ValueError):
                errors.append("P_15 is not a valid number")

        platnosc = fa.find(f"{ns}Platnosc")
        if platnosc is None:
            warnings.append("No Platnosc (payment) section found")

        nip1 = root.find(f"{ns}Podmiot1/{ns}DaneIdentyfikacyjne/{ns}NIP")
        if nip1 is not None and nip1.text:
            nip_clean = nip1.text.strip()
            if not nip_clean.isdigit() or len(nip_clean) != 10:
                warnings.append(f"Seller NIP '{nip_clean}' is not 10 digits")

        nip2 = root.find(f"{ns}Podmiot2/{ns}DaneIdentyfikacyjne/{ns}NIP")
        if nip2 is not None and nip2.text:
            nip_clean = nip2.text.strip()
            if not nip_clean.isdigit() or len(nip_clean) != 10:
                warnings.append(f"Buyer NIP '{nip_clean}' is not 10 digits")

    return errors, warnings


# ---------------------------------------------------------------------------
# Main conversion orchestrator
# ---------------------------------------------------------------------------


def convert_to_ksef(input_path, output_path, config=None):
    """Parse input, build KSeF XML, write to file, return summary."""
    if config is None:
        config = json.loads(json.dumps(DEFAULT_CONFIG))

    logger.info("Converting: %s -> %s", input_path, output_path)

    parsed = parse_input_xml(input_path)
    faktura, summary = build_ksef_xml(parsed, config)

    tree = ET.ElementTree(faktura)
    ET.indent(tree, space="\t")

    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".xml", dir=out_dir)
    try:
        os.close(tmp_fd)
        tree.write(tmp_path, encoding="utf-8", xml_declaration=True)

        errors, warnings = validate_ksef_xml(tmp_path)
        if warnings:
            for w in warnings:
                logger.warning("Validation warning: %s", w)
        if errors:
            for e in errors:
                logger.error("Validation error: %s", e)
            raise ValidationError(
                f"Output has {len(errors)} validation error(s) — see log above"
            )

        shutil.move(tmp_path, output_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    cur = summary.get("currency", config.get("defaults", {}).get("kod_waluty", "PLN"))
    logger.info("Conversion complete: %s", output_path)
    logger.info("  Lines: %d", summary["line_count"])
    logger.info("  Net:   %s %s", summary["total_net"], cur)
    logger.info("  VAT:   %s %s", summary["total_vat"], cur)
    logger.info("  Gross: %s %s", summary["total_gross"], cur)

    logger.info("Output validated OK")
    return summary


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def load_config(config_path):
    """Load JSON config, deep-merging with defaults."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))

    if config_path and os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            user = json.load(f)
        for key, value in user.items():
            if isinstance(value, dict) and key in config and isinstance(config[key], dict):
                config[key].update(value)
            else:
                config[key] = value
        logger.info("Configuration loaded from: %s", config_path)
    elif config_path:
        logger.warning("Config file not found: %s — using defaults", config_path)

    return config


def generate_sample_config(output_path="ksef_config.json"):
    """Write a documented sample config JSON for the user to fill in."""
    sample = {
        "_comment": "KSeF Converter configuration — fill in your company details",
        "seller_override": {
            "nip": "1234567890",
            "nazwa": "NAZWA FIRMY SP. Z O.O.",
            "adres_l1": "ul. Przykladowa 1",
            "adres_l2": "00-001 Warszawa",
            "kod_kraju": "PL",
            "email": "faktura@firma.pl",
            "telefon": "+48123456789"
        },
        "defaults": {
            "kod_waluty": "PLN",
            "miejsce_wystawienia": "Warszawa",
            "payment_days": 14,
            "forma_platnosci": "6",
            "system_info": "KSeFConverter v1.0"
        },
        "bank": {
            "nr_rb": "12345678901234567890123456",
            "nazwa_banku": "BANK PRZYKLADOWY S.A.",
            "opis": ""
        },
        "adnotacje": {
            "p_16": 2,
            "p_17": 2,
            "p_18": 2,
            "p_18a": 2,
            "p_23": 2
        },
        "vat_rate_overrides": {
            "_comment": "Map custom rate names to numeric values, e.g. 'special': 7"
        },
        "dodatkowy_opis": [
            {
                "klucz": "Notatka",
                "wartosc": "Przykladowy opis dodatkowy"
            }
        ]
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(sample, f, indent=4, ensure_ascii=False)
    logger.info("Konfiguracja wygenerowana: %s", output_path)
    logger.info("Otwórz plik i uzupełnij dane firmy przed konwersją.")


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------


def batch_convert(input_dir, output_dir, config):
    """Convert every XML in input_dir, writing results to output_dir."""
    in_path = Path(input_dir)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    xml_files = sorted(in_path.glob("*.xml"))
    if not xml_files:
        logger.warning("No XML files found in %s", input_dir)
        return {"success": 0, "failed": 0, "errors": []}

    results = {"success": 0, "failed": 0, "errors": []}

    for xf in xml_files:
        out_file = out_path / f"ksef_{xf.name}"
        try:
            convert_to_ksef(str(xf), str(out_file), config)
            results["success"] += 1
        except (ConversionError, ValidationError) as e:
            results["failed"] += 1
            results["errors"].append({"file": str(xf), "error": str(e)})
            logger.error("Failed: %s — %s", xf.name, e)
        except Exception as e:
            results["failed"] += 1
            results["errors"].append({"file": str(xf), "error": str(e)})
            logger.error("Unexpected error: %s — %s", xf.name, e)

    logger.info("Batch complete: %d OK, %d failed", results["success"], results["failed"])
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="KSeF Invoice Converter — Document-Invoice XML → KSeF FA(3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s input.xml output.xml
  %(prog)s input.xml output.xml -c ksef_config.json
  %(prog)s --batch input_dir/ output_dir/ -c ksef_config.json
  %(prog)s --init-config
  %(prog)s --validate output_ksef.xml
        """,
    )
    parser.add_argument("input", nargs="?", help="Input XML file (or dir with --batch)")
    parser.add_argument("output", nargs="?", help="Output XML file (or dir with --batch)")
    parser.add_argument("-c", "--config", help="Path to JSON configuration file")
    parser.add_argument("-b", "--batch", action="store_true", help="Batch-convert directory")
    parser.add_argument("--init-config", action="store_true", help="Generate sample config")
    parser.add_argument("--validate", metavar="FILE", help="Validate an existing KSeF XML")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-level logging")
    parser.add_argument("-q", "--quiet", action="store_true", help="Errors only")

    args = parser.parse_args()

    level = logging.ERROR if args.quiet else (logging.DEBUG if args.verbose else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.init_config:
        generate_sample_config(args.config or "ksef_config.json")
        return

    if args.validate:
        errors, warnings = validate_ksef_xml(args.validate)
        for w in warnings:
            print(f"  WARNING: {w}")
        for e in errors:
            print(f"  ERROR:   {e}")
        if not errors and not warnings:
            print("Validation OK — no issues found.")
        sys.exit(1 if errors else 0)

    if not args.input or not args.output:
        parser.error("input and output are required (unless --init-config or --validate)")

    config = load_config(args.config)

    if args.batch:
        results = batch_convert(args.input, args.output, config)
        sys.exit(1 if results["failed"] else 0)
    else:
        try:
            convert_to_ksef(args.input, args.output, config)
        except (ConversionError, ValidationError) as e:
            logger.error("Conversion failed: %s", e)
            sys.exit(1)
        except Exception as e:
            logger.error("Unexpected error: %s", e)
            if args.verbose:
                import traceback
                traceback.print_exc()
            sys.exit(1)


if __name__ == "__main__":
    main()
