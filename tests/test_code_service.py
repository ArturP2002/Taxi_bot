from app.services import code_service


def test_code_hash_roundtrip():
    h = code_service.hash_code(42, "123456")
    assert code_service.verify_code(42, "123456", h)
    assert not code_service.verify_code(42, "000000", h)


def test_qr_token_legacy():
    oid = 99
    t = code_service.build_qr_token(oid)
    assert code_service.verify_qr_token(t) == oid
    assert code_service.verify_qr_token("bad") is None


def test_parse_compact_and_deeplink():
    p = code_service.parse_verification_raw("DBR:16:482910")
    assert p is not None
    assert p.order_id == 16
    assert p.code == "482910"

    p2 = code_service.parse_verification_raw("vc_16_482910")
    assert p2 is not None
    assert p2.order_id == 16
    assert p2.code == "482910"

    p3 = code_service.parse_verification_raw(
        "https://t.me/MyBot?start=vc_16_482910"
    )
    assert p3 is not None
    assert p3.order_id == 16


def test_parse_six_digit_with_default_order():
    p = code_service.parse_verification_raw("482910", default_order_id=5)
    assert p is not None
    assert p.order_id == 5
    assert p.code == "482910"


def test_build_qr_png():
    png = code_service.render_qr_png("DBR:1:123456")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
