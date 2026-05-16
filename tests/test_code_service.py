from app.services import code_service


def test_code_hash_roundtrip():
    h = code_service.hash_code(42, "123456")
    assert code_service.verify_code(42, "123456", h)
    assert not code_service.verify_code(42, "000000", h)


def test_qr_token():
    oid = 99
    t = code_service.build_qr_token(oid)
    assert code_service.verify_qr_token(t) == oid
    assert code_service.verify_qr_token("bad") is None
