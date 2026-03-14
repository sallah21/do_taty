import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

def extract_nip(tax_id):
    """Extract NIP from tax ID (remove PL prefix if present)"""
    if tax_id and tax_id.startswith('PL'):
        return tax_id[2:]
    return tax_id

def convert_to_ksef(input_xml_path, output_xml_path):
    """Convert classic invoice XML to KSeF standard"""
    
    with open(input_xml_path, 'r', encoding='utf-8') as f:
        tree = ET.parse(f)
    root = tree.getroot()
    
    order = root.find('order')
    document = order.find('document')
    supplier = order.find('supplier')
    customer = order.find('customer')
    order_items = order.findall('orderItem')
    payment = order.find('payment')
    
    ns = "http://crd.gov.pl/wzor/2025/06/25/13775/"
    ET.register_namespace('', ns)
    faktura = ET.Element(f'{{{ns}}}Faktura')
    
    naglowek = ET.SubElement(faktura, 'Naglowek')
    kod_form = ET.SubElement(naglowek, 'KodFormularza', 
                             kodSystemowy="FA (3)", wersjaSchemy="1-0E")
    kod_form.text = "FA"
    ET.SubElement(naglowek, 'WariantFormularza').text = "3"
    ET.SubElement(naglowek, 'DataWytworzeniaFa').text = datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')
    ET.SubElement(naglowek, 'SystemInfo').text = "InvoiceConverter"
    
    podmiot1 = ET.SubElement(faktura, 'Podmiot1')
    dane_ident1 = ET.SubElement(podmiot1, 'DaneIdentyfikacyjne')
    ET.SubElement(dane_ident1, 'NIP').text = extract_nip(supplier.find('dic').text)
    ET.SubElement(dane_ident1, 'Nazwa').text = supplier.find('company').text
    
    adres1 = ET.SubElement(podmiot1, 'Adres')
    ET.SubElement(adres1, 'KodKraju').text = 'PL'
    street1 = supplier.find('street').text or ''
    city1 = supplier.find('city').text or ''
    psc1 = supplier.find('psc').text or ''
    if street1:
        ET.SubElement(adres1, 'AdresL1').text = street1
    if city1 or psc1:
        ET.SubElement(adres1, 'AdresL2').text = f"{psc1} {city1}".strip()
    
    podmiot2 = ET.SubElement(faktura, 'Podmiot2')
    dane_ident2 = ET.SubElement(podmiot2, 'DaneIdentyfikacyjne')
    ET.SubElement(dane_ident2, 'NIP').text = extract_nip(customer.find('dic').text)
    ET.SubElement(dane_ident2, 'Nazwa').text = customer.find('company').text.upper()
    
    adres2 = ET.SubElement(podmiot2, 'Adres')
    ET.SubElement(adres2, 'KodKraju').text = 'PL'
    ET.SubElement(adres2, 'AdresL1').text = customer.find('street').text
    psc2 = customer.find('psc').text
    city2 = customer.find('city').text
    ET.SubElement(adres2, 'AdresL2').text = f"{psc2} {city2}"
    
    fa = ET.SubElement(faktura, 'Fa')
    ET.SubElement(fa, 'KodWaluty').text = 'PLN'
    
    doc_date = document.get('date')
    ET.SubElement(fa, 'P_1').text = doc_date
    ET.SubElement(fa, 'P_2').text = document.get('number')
    ET.SubElement(fa, 'P_6').text = doc_date
    
    total_net = 0
    total_vat = 0
    
    for item in order_items:
        price = float(item.get('price'))
        quantity = float(item.get('quantity'))
        net_value = price * quantity
        
        vat_rate = 23 if item.get('rateVAT') == 'high' else 0
        vat_value = net_value * (vat_rate / 100)
        
        total_net += net_value
        total_vat += vat_value
    
    ET.SubElement(fa, 'P_13_1').text = f"{total_net:.2f}"
    ET.SubElement(fa, 'P_14_1').text = f"{total_vat:.2f}"
    ET.SubElement(fa, 'P_15').text = f"{total_net + total_vat:.2f}"
    
    adnotacje = ET.SubElement(fa, 'Adnotacje')
    ET.SubElement(adnotacje, 'P_16').text = '2'
    ET.SubElement(adnotacje, 'P_17').text = '2'
    ET.SubElement(adnotacje, 'P_18').text = '2'
    ET.SubElement(adnotacje, 'P_18A').text = '2'
    zwolnienie = ET.SubElement(adnotacje, 'Zwolnienie')
    ET.SubElement(zwolnienie, 'P_19N').text = '1'
    nowe_srodki = ET.SubElement(adnotacje, 'NoweSrodkiTransportu')
    ET.SubElement(nowe_srodki, 'P_22N').text = '1'
    ET.SubElement(adnotacje, 'P_23').text = '2'
    pmarzy = ET.SubElement(adnotacje, 'PMarzy')
    ET.SubElement(pmarzy, 'P_PMarzyN').text = '1'
    
    ET.SubElement(fa, 'RodzajFaktury').text = 'VAT'
    
    for idx, item in enumerate(order_items, 1):
        fa_wiersz = ET.SubElement(fa, 'FaWiersz')
        ET.SubElement(fa_wiersz, 'NrWierszaFa').text = str(idx)
        ET.SubElement(fa_wiersz, 'P_6A').text = doc_date
        ET.SubElement(fa_wiersz, 'P_7').text = item.text.strip()
        ET.SubElement(fa_wiersz, 'P_8A').text = item.get('unit')
        ET.SubElement(fa_wiersz, 'P_8B').text = item.get('quantity')
        ET.SubElement(fa_wiersz, 'P_9A').text = item.get('price')
        
        price = float(item.get('price'))
        quantity = float(item.get('quantity'))
        ET.SubElement(fa_wiersz, 'P_11').text = f"{price * quantity:.2f}"
        ET.SubElement(fa_wiersz, 'P_12').text = '23'
    
    platnosc = ET.SubElement(fa, 'Platnosc')
    termin_platnosci = ET.SubElement(platnosc, 'TerminPlatnosci')
    payment_date = datetime.strptime(doc_date, '%Y-%m-%d') + timedelta(days=14)
    ET.SubElement(termin_platnosci, 'Termin').text = payment_date.strftime('%Y-%m-%d')
    ET.SubElement(platnosc, 'FormaPlatnosci').text = '6'
    
    tree_out = ET.ElementTree(faktura)
    ET.indent(tree_out, space='\t')
    tree_out.write(output_xml_path, encoding='utf-8', xml_declaration=True)
    
    print(f"Conversion complete: {output_xml_path}")
    print(f"Total net: {total_net:.2f} PLN")
    print(f"Total VAT: {total_vat:.2f} PLN")
    print(f"Total gross: {total_net + total_vat:.2f} PLN")

if __name__ == "__main__":
    convert_to_ksef('normalny.xml', 'output_ksef.xml')
