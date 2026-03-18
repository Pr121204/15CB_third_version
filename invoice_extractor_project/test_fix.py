import sys
import os
import json

# Add project path to sys.path
sys.path.append(r'c:\Users\HP\Desktop\15CB_third_version-main\invoice_extractor_project')

from extractors import bosch_germany

ocr_text = """coverpage

Cover for invoice

Invoice No.

Bosch Global Software Technologies
Private Limited

123 Industrial Estate, Hosur Road
560095 BANGALORE-KORAMANGALA
INDIEN

19002087

Robert Bosch GmbH

Invoice 1/ 2
Invoice No. : 19002087
Date Invoice : 09.10.2025
Supplier code
Payer : 1000000729
Bosch Global Software Technologies customer no. : 1000000730
Private Limited Ship to : 1000021595
123, Industrial Layout,Hosur Road
560095 BANGALORE-KORAMANGALA
INDIA Contact addresses
Sales

Accounting : GS/OSP-SP251 Shevchenko A.
Our VAT ID No : DE811128135
Your VAT ID No :

Dispatch address : Bosch Global Software Technologies, Private Limited, 123, Industrial Layout,Hosur Road,
IN-560095 Bangalore

Correspondence : Robert Bosch GmbH, GS/OBC-EMEA1, Postfach 10 60 50, DE-70049 Stuttgart
Company address : Robert Bosch GmbH, Robert-Bosch-Platz 1, DE-70839 Gerlingen-Schillerhoehe
Shipping point ad. :, , “=

Item N°. Bosch Partnumber Customer Partnumber Quantity Price Amount
Material Description Unit Oty Price unit Currency

Ctry Origin Net weight/kg Rebate

GT License

Ansprechpartner (Abteilung/Person)
PS/EVC3 Drewes; PS/EVC2 Schwarz
Antony, Placid (MS/ECJ41-PS)

PAN: AADCR1702Q
Our order number : 30412977 Your order number : 87007536 Date

o1 F018 .094.30G 1 2,050.00 2,050.00
GT License PC 1 EUR

Date of supply (service/ goods delivery): 31.10.2025

Value of goods: EUR 2,050.00

Bank Details : DEUTSCHE BANK AG, BIC DEUTDESSXXX, IBAN DE32 6007 0070 0119 0677 14

Registered Office: Stuttgart, Registration Court: Amtsgericht Stuttgart, HRB 14000;
Chairman of the Supervisory Board: Prof. Dr. Stefan Asenkerschbaumer;
Managing Directors: Dr. Stefan Hartung, Dr. Christian Fischer, Dr. Markus Forschner,

Stefan Grosch, Dr. Markus Heyn, Dr. Frank Meyer, Katja von Raven, Dr. Tanja Rueckert

Robert Bosch GmbH BOSCH

Invoice 2/ 2
Bosch Global Software Technologies Invoice no. : 19002087
Private Limited Date Invoice : 09.10.2025
123, Industrial Layout,Hosur Road Supplier code :
560095 BANGALORE-KORAMANGALA Payer : 1000000729
INDIA Customer No. : 1000000730
Ship to : 1000021595
Item N°. Bosch Partnumber Customer Partnumber Quantity Price Amount
Material Description Unit Qty Price unit Currency
Ctry Origin Net weight/kg Rebate
Net amount: EUR 2,050.00
Value Added Tax (VAT) : 0.000 % EUR 0.00
Invoice amount Fi EUR 2,050.00

Reverse Charge

Steuerschuldnerschaft des Leistungsempfangers

Payment conditions

60 days net

Up to 08.12.2025 without deduction

Pavment reductions can cnlyv be taken based on payment terms and conditions already agreed with vou.

Payment address :
ROBERT BOSCH GMBH
GS/OBC13-EMEAL
POSTFACH 10 60 50
70049 Stuttgart

Incoterms 2020:FCA Bosch Plant
Customs Tarif N° Weight/Kg Amount

0.000 2,050.00 EUR
This document is legally valid without a signature

Bank Details : DEUTSCHE BANK AG, BIC DEUTDESSXXX, IBAN DE32 6007 0070 0119 0677 14

Registered Office: Stuttgart, Registration Court: Amtsgericht Stuttgart, HRB 14000;
Chairman of the Supervisory Board: Prof. Dr. Stefan Asenkerschbaumer;
Managing Directors: Dr. Stefan Hartung, Dr. Christian Fischer, Dr. Markus Forschner,

Stefan Grosch, Dr. Markus Heyn, Dr. Frank Meyer, Katja von Raven, Dr. Tanja Rueckert

From: System.P81@de.bosch.com
Sent: October 09, 2025

To: einvoice_65206520@de.bosch.com
CC:

Subject: Invoice-0019002087

<A>Hello Customer, please find the original invoice 19002087 for the
respective customer 1000000730"""

data = bosch_germany.extract(ocr_text)
print(json.dumps(data, indent=2))
