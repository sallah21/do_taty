# KSeF Invoice Converter

Prosty skrypt w Pythonie do konwersji klasycznych plików XML w formacie `eform/order` do struktury XML zgodnej z polskim standardem KSeF FA(3).

## Co robi skrypt

Skrypt:

- odczytuje wejściowy plik XML z fakturą / zamówieniem,
- wykrywa kodowanie pliku wejściowego,
- mapuje dane sprzedawcy, nabywcy, pozycji i płatności,
- buduje dokument XML w formacie KSeF FA(3),
- zapisuje wynik do pliku wyjściowego,
- potrafi walidować wygenerowany plik,
- obsługuje przetwarzanie pojedynczych plików i tryb wsadowy,
- umożliwia użycie pliku konfiguracyjnego JSON.

## Wymagania

- Python 3.9 lub nowszy
- system z uruchomionym `python3`

Skrypt korzysta wyłącznie ze standardowej biblioteki Pythona, więc nie wymaga instalowania dodatkowych pakietów.

## Pliki w projekcie

- `invoice_converter.py` — główny skrypt konwertujący
- `ksef_config.json` — przykładowa konfiguracja użytkownika

## Szybki start

### Konwersja jednego pliku

```bash
python3 invoice_converter.py input.xml output.xml
```

### Konwersja z użyciem konfiguracji

```bash
python3 invoice_converter.py input.xml output.xml --config ksef_config.json
```

### Walidacja gotowego pliku KSeF

```bash
python3 invoice_converter.py --validate output_ksef.xml
```

### Generowanie przykładowej konfiguracji

```bash
python3 invoice_converter.py --init-config
```

### Przetwarzanie wsadowe katalogu

```bash
python3 invoice_converter.py --batch input_dir output_dir --config ksef_config.json
```

## Format danych wejściowych

Skrypt zakłada, że wejściowy XML zawiera strukturę podobną do:

```xml
<eform>
  <order>
    <document number="..." date="...">...</document>
    <supplier>...</supplier>
    <customer>...</customer>
    <orderItem ...>...</orderItem>
    <payment ...>...</payment>
  </order>
</eform>
```

W szczególności wykorzystywane są:

- `document/@number`
- `document/@date`
- `supplier/dic`
- `supplier/company`
- `customer/dic`
- `customer/company`
- `orderItem/@price`
- `orderItem/@quantity`
- `orderItem/@unit`
- `orderItem/@rateVAT`
- `payment/@payType`

## Konfiguracja `ksef_config.json`

Plik konfiguracyjny pozwala nadpisywać dane sprzedawcy i ustawienia domyślne.

### Główne sekcje

#### `seller_override`
Dane sprzedawcy wpisywane do pliku wynikowego.

Przykładowe pola:

- `nip`
- `nazwa`
- `adres_l1`
- `adres_l2`
- `kod_kraju`
- `email`
- `telefon`

#### `defaults`
Ustawienia domyślne dokumentu.

Przykładowe pola:

- `kod_waluty`
- `miejsce_wystawienia`
- `payment_days`
- `forma_platnosci`
- `system_info`

#### `bank`
Dane rachunku bankowego dodawane do sekcji płatności.

Przykładowe pola:

- `nr_rb`
- `nazwa_banku`
- `opis`

#### `adnotacje`
Domyślne wartości wybranych pól adnotacji KSeF.

#### `vat_rate_overrides`
Pozwala mapować niestandardowe oznaczenia VAT na konkretne stawki liczbowe.

#### `dodatkowy_opis`
Lista dodatkowych wpisów `DodatkowyOpis` dodawanych do faktury.

## Przykładowy przebieg pracy

### 1. Wygeneruj konfigurację

```bash
python3 invoice_converter.py --init-config
```

### 2. Uzupełnij dane firmy w `ksef_config.json`

Ustaw przede wszystkim:

- NIP sprzedawcy
- nazwę firmy
- adres
- e-mail i telefon
- numer rachunku bankowego

### 3. Uruchom konwersję

```bash
python3 invoice_converter.py normalny.xml output_ksef.xml --config ksef_config.json
```

### 4. Zweryfikuj wynik

```bash
python3 invoice_converter.py --validate output_ksef.xml
```

## Logowanie

Dostępne są dwa przełączniki logowania:

```bash
python3 invoice_converter.py input.xml output.xml --verbose
python3 invoice_converter.py input.xml output.xml --quiet
```

- `--verbose` — szczegółowe logi diagnostyczne
- `--quiet` — wyświetlanie tylko błędów

## Obsługiwane mapowania

### VAT

Domyślne mapowanie stawek VAT:

- `high` → `23`
- `low` → `8`
- `reduced` → `5`
- `zero` → `0`
- `none` → zwolnienie
- `exempt` → zwolnienie
- `zw` → zwolnienie

### Forma płatności

Domyślne mapowanie typów płatności:

- `cash`, `gotowka` → `1`
- `card`, `karta` → `2`
- `voucher`, `bon` → `3`
- `cheque`, `check`, `czek` → `4`
- `credit`, `kredyt` → `5`
- `transfer`, `bank_transfer`, `bank`, `przelew` → `6`
- `mobile`, `blik` → `7`

## Ograniczenia i uwagi

- Skrypt nie wykonuje pełnej walidacji XSD, tylko walidację logiczną wybranych pól.
- Poprawność końcowego XML należy zweryfikować w docelowym środowisku KSeF lub oficjalnym narzędziu walidującym.
- Dane wejściowe muszą mieć zgodną strukturę XML.
- W przypadku nieprawidłowego lub niespójnego kodowania znaki narodowe mogą zostać uszkodzone już na etapie odczytu pliku wejściowego.
- Szczegóły wykrytych błędów implementacyjnych opisano w pliku `BUG_REPORT.md`.

## Możliwe zastosowania

- konwersja eksportów XML z zewnętrznych systemów sprzedażowych,
- przygotowanie danych do dalszej integracji z KSeF,
- walidacja i testowanie mapowania danych faktur do struktury FA(3),
- przetwarzanie wielu dokumentów w trybie wsadowym.

## Uruchomienie na przykładowych plikach z repozytorium

```bash
python3 invoice_converter.py normalny.xml output_ksef.xml --config ksef_config.json
```

Po wykonaniu polecenia wynik zostanie zapisany do `output_ksef.xml`.
