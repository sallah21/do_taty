#!/usr/bin/env python3
"""
KSeF Invoice Converter - Production Edition
Converts classic eform/order XML invoices to Polish KSeF FA(3) standard.

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
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
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
    Decimal("0"):  ("P_13_6", None),
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
        "kod_kraju": "PL",
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
    """Extract clean NIP from a tax ID string (strip country prefix)."""
    if not tax_id:
        return None
    tax_id = tax_id.strip()
    if len(tax_id) > 2 and tax_id[:2].isalpha():
        return tax_id[2:]
    return tax_id


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


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_input_xml(file_path):
    """Parse an eform/order XML file and return its main elements."""
    encoding = detect_encoding(file_path)
    logger.info("Detected encoding: %s for %s", encoding, file_path)

    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        tree = ET.parse(f)
    root = tree.getroot()

    order = root.find("order")
    if order is None:
        raise ConversionError(f"No <order> element found in {file_path}")

    document = order.find("document")
    supplier = order.find("supplier")
    customer = order.find("customer")
    items = order.findall("orderItem")
    payment = order.find("payment")

    if document is None:
        raise ConversionError("Missing <document> element")
    if not items:
        raise ConversionError("No <orderItem> elements found")

    return {
        "document": document,
        "supplier": supplier,
        "customer": customer,
        "items": items,
        "payment": payment,
    }


# ---------------------------------------------------------------------------
# KSeF XML builder
# ---------------------------------------------------------------------------


def build_ksef_xml(parsed, config):
    """Build a complete KSeF FA(3) XML tree from parsed input and config."""
    doc = parsed["document"]
    supplier = parsed["supplier"]
    customer = parsed["customer"]
    items = parsed["items"]
    payment = parsed["payment"]

    cfg_seller = config.get("seller_override", {})
    cfg_defaults = config.get("defaults", {})
    cfg_bank = config.get("bank", {})
    cfg_adnotacje = config.get("adnotacje", {})
    cfg_vat = config.get("vat_rate_overrides", {})

    doc_number = get_attr(doc, "number")
    doc_date = get_attr(doc, "date")
    if not doc_date:
        raise ConversionError("Document date is required")
    if not doc_number:
        raise ConversionError("Document number is required")

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
        datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    ET.SubElement(naglowek, "SystemInfo").text = cfg_defaults.get(
        "system_info", "KSeFConverter v1.0"
    )

    # --- Podmiot1 (Seller) ---
    podmiot1 = ET.SubElement(faktura, "Podmiot1")
    dane1 = ET.SubElement(podmiot1, "DaneIdentyfikacyjne")

    seller_nip = (
        cfg_seller.get("nip") or extract_nip(get_text(supplier, "dic"))
    )
    seller_name = cfg_seller.get("nazwa") or get_text(supplier, "company")
    if not seller_nip:
        raise ConversionError(
            "Seller NIP is required — set it in config or ensure input XML has <dic>"
        )

    ET.SubElement(dane1, "NIP").text = seller_nip
    ET.SubElement(dane1, "Nazwa").text = seller_name

    adres1 = ET.SubElement(podmiot1, "Adres")
    seller_country = (
        cfg_seller.get("kod_kraju")
        or extract_country(get_text(supplier, "dic"))
        or "PL"
    )
    ET.SubElement(adres1, "KodKraju").text = seller_country

    a1_l1 = cfg_seller.get("adres_l1") or get_text(supplier, "street")
    a1_l2 = cfg_seller.get("adres_l2")
    if not a1_l2:
        psc = get_text(supplier, "psc")
        city = get_text(supplier, "city")
        if psc or city:
            a1_l2 = f"{psc} {city}".strip()
    if a1_l1:
        ET.SubElement(adres1, "AdresL1").text = a1_l1
    if a1_l2:
        ET.SubElement(adres1, "AdresL2").text = a1_l2

    seller_email = cfg_seller.get("email") or get_text(supplier, "email")
    seller_phone = cfg_seller.get("telefon") or get_text(supplier, "tel")
    if seller_email or seller_phone:
        kontakt1 = ET.SubElement(podmiot1, "DaneKontaktowe")
        if seller_email:
            ET.SubElement(kontakt1, "Email").text = seller_email
        if seller_phone:
            ET.SubElement(kontakt1, "Telefon").text = seller_phone

    # --- Podmiot2 (Buyer) ---
    podmiot2 = ET.SubElement(faktura, "Podmiot2")
    dane2 = ET.SubElement(podmiot2, "DaneIdentyfikacyjne")

    buyer_dic = get_text(customer, "dic")
    buyer_nip = extract_nip(buyer_dic)
    buyer_country = extract_country(buyer_dic) if buyer_dic else "PL"
    buyer_name = get_text(customer, "company")
    if not buyer_nip:
        raise ConversionError("Buyer NIP / tax ID is required in input XML <dic>")

    if buyer_country == "PL":
        ET.SubElement(dane2, "NIP").text = buyer_nip
    else:
        ET.SubElement(dane2, "KodUE").text = buyer_country
        ET.SubElement(dane2, "NrVatUE").text = buyer_nip

    ET.SubElement(dane2, "Nazwa").text = buyer_name.upper()

    adres2 = ET.SubElement(podmiot2, "Adres")
    ET.SubElement(adres2, "KodKraju").text = buyer_country
    buyer_street = get_text(customer, "street")
    buyer_psc = get_text(customer, "psc")
    buyer_city = get_text(customer, "city")
    if buyer_street:
        ET.SubElement(adres2, "AdresL1").text = buyer_street
    if buyer_psc or buyer_city:
        ET.SubElement(adres2, "AdresL2").text = f"{buyer_psc} {buyer_city}".strip()

    buyer_email = get_text(customer, "email")
    buyer_phone = get_text(customer, "tel")
    if buyer_email or buyer_phone:
        kontakt2 = ET.SubElement(podmiot2, "DaneKontaktowe")
        if buyer_email:
            ET.SubElement(kontakt2, "Email").text = buyer_email
        if buyer_phone:
            ET.SubElement(kontakt2, "Telefon").text = buyer_phone

    ET.SubElement(podmiot2, "JST").text = "2"
    ET.SubElement(podmiot2, "GV").text = "2"

    # --- Fa ---
    fa = ET.SubElement(faktura, "Fa")
    currency = cfg_defaults.get("kod_waluty", "PLN")
    ET.SubElement(fa, "KodWaluty").text = currency
    ET.SubElement(fa, "P_1").text = doc_date

    place = cfg_defaults.get("miejsce_wystawienia")
    if place:
        ET.SubElement(fa, "P_1M").text = place

    ET.SubElement(fa, "P_2").text = doc_number
    ET.SubElement(fa, "P_6").text = doc_date

    # --- Calculate VAT buckets per rate ---
    vat_buckets = {}
    line_data = []
    has_exempt = False

    for item in items:
        price = money(get_attr(item, "price", "0"))
        qty_str = get_attr(item, "quantity", "1")
        quantity = Decimal(qty_str)
        rate_str = get_attr(item, "rateVAT", "high")
        vat_rate = resolve_vat_rate(rate_str, cfg_vat)

        net_value = money(price * quantity)

        if vat_rate is not None:
            vat_value = money(net_value * vat_rate / Decimal("100"))
            ksef_rate_display = str(int(vat_rate)) if vat_rate == int(vat_rate) else str(vat_rate)
        else:
            vat_value = money(0)
            ksef_rate_display = "zw"
            has_exempt = True

        bucket = vat_buckets.setdefault(vat_rate, {"net": Decimal("0"), "vat": Decimal("0")})
        bucket["net"] += net_value
        bucket["vat"] += vat_value

        line_data.append({
            "description": (item.text or "").strip(),
            "unit": get_attr(item, "unit", "szt"),
            "quantity": qty_str,
            "price": str(price),
            "net_value": str(net_value),
            "vat_rate_display": ksef_rate_display,
            "date": get_attr(item, "date") or doc_date,
            "ean": get_attr(item, "EAN"),
            "code": get_attr(item, "code"),
            "remark": get_attr(item, "remark").strip(),
        })

    # Write P_13_x / P_14_x fields — sorted: 23%, 8%, 5%, special, 0%, exempt
    rate_sort_key = lambda r: (r is None, -(r or Decimal("0")))
    total_net = Decimal("0")
    total_vat = Decimal("0")

    for rate in sorted(vat_buckets, key=rate_sort_key):
        bucket = vat_buckets[rate]
        net = money(bucket["net"])
        vat = money(bucket["vat"])
        total_net += net
        total_vat += vat

        p13, p14 = get_ksef_vat_fields(rate)
        ET.SubElement(fa, p13).text = str(net)
        if p14 and vat > 0:
            ET.SubElement(fa, p14).text = str(vat)

    total_gross = money(total_net + total_vat)
    ET.SubElement(fa, "P_15").text = str(total_gross)

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
    ET.SubElement(zwolnienie, "P_19N").text = "1" if not has_exempt else "2"

    nowe_srodki = ET.SubElement(adnotacje, "NoweSrodkiTransportu")
    ET.SubElement(nowe_srodki, "P_22N").text = "1"
    ET.SubElement(adnotacje, "P_23").text = str(cfg_adnotacje.get("p_23", 2))
    pmarzy = ET.SubElement(adnotacje, "PMarzy")
    ET.SubElement(pmarzy, "P_PMarzyN").text = "1"

    # --- RodzajFaktury ---
    ET.SubElement(fa, "RodzajFaktury").text = "VAT"

    # --- DodatkowyOpis ---
    for opis in config.get("dodatkowy_opis", []):
        do = ET.SubElement(fa, "DodatkowyOpis")
        ET.SubElement(do, "Klucz").text = opis.get("klucz", "Notatka")
        ET.SubElement(do, "Wartosc").text = opis.get("wartosc", "")

    # --- FaWiersz ---
    for idx, ld in enumerate(line_data, 1):
        fw = ET.SubElement(fa, "FaWiersz")
        ET.SubElement(fw, "NrWierszaFa").text = str(idx)
        ET.SubElement(fw, "UU_ID").text = str(uuid.uuid4()).replace("-", "")[:32]
        ET.SubElement(fw, "P_6A").text = ld["date"]
        ET.SubElement(fw, "P_7").text = ld["description"]
        ET.SubElement(fw, "P_8A").text = ld["unit"]
        ET.SubElement(fw, "P_8B").text = ld["quantity"]
        ET.SubElement(fw, "P_9A").text = ld["price"]
        ET.SubElement(fw, "P_11").text = ld["net_value"]
        ET.SubElement(fw, "P_12").text = ld["vat_rate_display"]

    # --- Platnosc ---
    platnosc = ET.SubElement(fa, "Platnosc")

    pay_type_input = get_attr(payment, "payType", "") if payment is not None else ""
    payment_days = cfg_defaults.get("payment_days", 14)

    try:
        pay_date = datetime.strptime(doc_date, "%Y-%m-%d") + timedelta(days=payment_days)
    except ValueError as e:
        raise ConversionError(f"Invalid document date format: {doc_date}") from e

    termin = ET.SubElement(platnosc, "TerminPlatnosci")
    ET.SubElement(termin, "Termin").text = pay_date.strftime("%Y-%m-%d")

    forma = cfg_defaults.get("forma_platnosci") or pay_type_input or "6"
    ET.SubElement(platnosc, "FormaPlatnosci").text = resolve_payment_type(forma)

    bank_nr = cfg_bank.get("nr_rb", "")
    bank_name = cfg_bank.get("nazwa_banku", "")
    if bank_nr:
        rb = ET.SubElement(platnosc, "RachunekBankowy")
        ET.SubElement(rb, "NrRB").text = bank_nr
        if bank_name:
            ET.SubElement(rb, "NazwaBanku").text = bank_name
        bank_desc = cfg_bank.get("opis", "")
        if bank_desc:
            ET.SubElement(rb, "OpisRachunku").text = bank_desc

    summary = {
        "total_net": total_net,
        "total_vat": total_vat,
        "total_gross": total_gross,
        "line_count": len(line_data),
        "vat_buckets": {
            str(k): {"net": str(money(v["net"])), "vat": str(money(v["vat"]))}
            for k, v in vat_buckets.items()
        },
    }
    return faktura, summary


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
                    ("4", "special"), ("6", "0%"), ("7", "exempt"),
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
        config = DEFAULT_CONFIG.copy()

    logger.info("Converting: %s -> %s", input_path, output_path)

    parsed = parse_input_xml(input_path)
    faktura, summary = build_ksef_xml(parsed, config)

    tree = ET.ElementTree(faktura)
    ET.indent(tree, space="\t")
    tree.write(output_path, encoding="utf-8", xml_declaration=True)

    cur = config.get("defaults", {}).get("kod_waluty", "PLN")
    logger.info("Conversion complete: %s", output_path)
    logger.info("  Lines: %d", summary["line_count"])
    logger.info("  Net:   %s %s", summary["total_net"], cur)
    logger.info("  VAT:   %s %s", summary["total_vat"], cur)
    logger.info("  Gross: %s %s", summary["total_gross"], cur)

    errors, warnings = validate_ksef_xml(output_path)
    if warnings:
        for w in warnings:
            logger.warning("Validation warning: %s", w)
    if errors:
        for e in errors:
            logger.error("Validation error: %s", e)
        raise ValidationError(
            f"Output has {len(errors)} validation error(s) — see log above"
        )

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
    print(f"Sample configuration generated: {output_path}")
    print("Edit this file with your company details before converting invoices.")


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
        description="KSeF Invoice Converter — eform/order XML → KSeF FA(3)",
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
