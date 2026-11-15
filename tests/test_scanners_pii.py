import pytest
from hera.privacy.scanners.pii import field_combination, luhn_ok, scan_pii, scan_pii_combined


def kinds(text):
    return scan_pii(text.encode())


@pytest.mark.parametrize("text", ["ines.varga@quillmere.io", "a+tag@mail.brantlow.co.uk", "x_y@fennick.ca"])
def test_email_positive(text):
    assert kinds(f"write to {text} soon") == ["pii:email"]


@pytest.mark.parametrize("text", [
    "a@example.com", "a@example.org", "a@example.net", "a@mail.example.com", "a@example.co.uk",
    "a@test.org", "a@test.local", "a@localhost", "a@host.invalid", "a@host.test", "a@host.example",
    "lodash@4.17.21", "image@2x.png", "@decorator", "user@",
])
def test_email_exceptions(text):
    assert "pii:email" not in kinds(f"see {text} ok")


@pytest.mark.parametrize("text", [
    "(212) 555-0275", "212-555-0275", "212.555.0275", "+1 212 555 0275", "+44 20 7946 0958",
    "+81 3-5555-0275", "+49 30 5555 0275", "+63 917 555 0275",
])
def test_phone_positive(text):
    assert "pii:phone" in kinds(f"call {text} now")


@pytest.mark.parametrize("text", [
    "2125550275", "1.2.3.4", "v10.20.30", "192.168.100.1", "2026-11-14", "2026-11-14T12:30:45Z",
    "1731585045123", "(212) 555-0100", "212-555-0142", "+1 212 555 0199", "pi 3.14159265358979",
    "ISBN 978-3-16-148410-0",
])
def test_phone_exceptions(text):
    assert "pii:phone" not in kinds(f"see {text} ok")


@pytest.mark.parametrize("text", [
    "1420 Marlow Street", "22 Harbour Road, Springfield", "305 Oak Blvd", "78 Elm Lane", "9 Cedar Way",
    "1600 Pine Avenue Toronto ON M5V 2T6", "12 King St.", "5 Oak Dr", "7 5th Ave",
    "ship to M5V 2T6", "postal code: SW1A 1AA", "mailing address ZIP 94107", "Zip: 94107-1234",
])
def test_address_positive(text):
    assert "pii:postal_address" in kinds(f"deliver: {text}")


@pytest.mark.parametrize("text", [
    "M5V 2T6", "94107", "SW1A 1AA", "it took 5 minutes to drive", "page 12 of 40",
    "10 ways to improve", "the way", "In 2024 we shipped 12 releases",
])
def test_address_exceptions(text):
    assert "pii:postal_address" not in kinds(f"note {text}")


def make_card(prefix, length):
    body = prefix + "".join(str((i * 7 + 3) % 10) for i in range(length - len(prefix) - 1))
    for check in "0123456789":
        if luhn_ok(body + check):
            return body + check


@pytest.mark.parametrize("prefix,length", [("4", 16), ("51", 16), ("37", 15), ("6011", 16), ("4", 13), ("4", 19)])
def test_card_positive(prefix, length):
    n = make_card(prefix, length)
    assert "pii:payment_card" in kinds(f"card {n}")
    spaced = " ".join(n[i:i + 4] for i in range(0, len(n), 4))
    assert "pii:payment_card" in kinds(f"card {spaced}")
    assert "pii:payment_card" in kinds("card " + "-".join(n[i:i + 4] for i in range(0, len(n), 4)))


def test_card_exceptions():
    n = make_card("4", 16)
    bad = n[:-1] + str((int(n[-1]) + 1) % 10)
    assert not luhn_ok(bad)
    assert "pii:payment_card" not in kinds(f"card {bad}")
    assert "pii:payment_card" not in kinds("n 4444 4444 4444 4444 4444 4444")
    assert "pii:payment_card" not in kinds("ts 1731585045123")
    assert "pii:payment_card" not in kinds("n 0000000000000000")


@pytest.mark.parametrize("text", [
    "1Z" + "999AA10123456784", "9400111899223197428490", "9205590164917312751089", "9361289878905919630610",
    "9501101053802345678901"[:20], "tracking 123456789012", "FedEx: 123456789012345", "shipment 999988887777",
])
def test_tracking_positive(text):
    assert "pii:tracking_number" in kinds(f"parcel {text}")


@pytest.mark.parametrize("text", [
    "123456789012", "ref 123456789012 for the build log", "1Z12", "8400111899223197428490",
    "tracking 12345678901",
])
def test_tracking_exceptions(text):
    assert "pii:tracking_number" not in kinds(f"parcel {text}")


@pytest.mark.parametrize("text", [
    "Order #A1B2C3D4", "order_id: 884213", "invoice INV-20481", "Order no. 58213-77", "order 99120345",
    "Order ID: 7F3K9X2", "ORDER# 55512ab", "Invoice: 1234567",
])
def test_order_id_positive(text):
    assert "pii:order_id" in kinds(text)


@pytest.mark.parametrize("text", [
    "In order to continue", "order of operations", "sorted in descending order by date",
    "order 1234", "order ABCDEFGH", "invoice due soon", "order      12345",
])
def test_order_id_exceptions(text):
    assert "pii:order_id" not in kinds(text)


def test_field_combination():
    assert field_combination(["pii:email"]) == []
    assert field_combination(["pii:email", "pii:email"]) == []
    assert field_combination(["pii:email", "pii:phone"]) == ["pii:combination"]
    assert field_combination(["pii:email", "secret:jwt"]) == []
    assert field_combination(["pii:combination"]) == []
    out = scan_pii_combined(b"ines@quillmere.io (212) 555-0275")
    assert out == ["pii:email", "pii:phone", "pii:combination"]
    assert scan_pii_combined(b"ines@quillmere.io") == ["pii:email"]


def test_zero_width_stripped_and_no_text_in_findings():
    assert "pii:email" in kinds("ines​@quill‌mere.io")
    for f in scan_pii_combined(b"ines@quillmere.io 1420 Marlow Street"):
        assert "ines" not in f and "Marlow" not in f
