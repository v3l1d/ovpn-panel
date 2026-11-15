import pytest

from ovpnpanel import settings, validate

INJECTIONS = ["a\nb", "a\rb", "a b", 'a"b', "a;b", "a$(id)b", "a`id`b", "a\x00b", "a\\b", "a'b", "a#b", "a|b", "a&b"]


@pytest.mark.parametrize("value", ["anna", "marco-laptop", "a_b.c", "x1", "a" * 32])
def test_good_names(value):
    assert validate.name(value) == value


@pytest.mark.parametrize("value", ["", "a", "A", "-x", "x" * 33, "ü", None, 5, "name\n"] + INJECTIONS)
def test_bad_names(value):
    with pytest.raises(validate.ValidationError):
        validate.name(value)


def test_a_trailing_newline_does_not_slip_through_the_anchors():
    """"$" would accept "anna\\n"; fullmatch must not."""
    with pytest.raises(validate.ValidationError):
        validate.name("anna\n")
    with pytest.raises(validate.ValidationError):
        validate.cn("anna_abcdef\n")
    assert validate.host("example.net\n") == "example.net"  # hosts are stripped: no newline in what is returned


@pytest.mark.parametrize("value", ["anna_a1b2c3", "marco-laptop_000000", "x1_abcdef"])
def test_good_certificate_names(value):
    assert validate.cn(value) == value


@pytest.mark.parametrize("value", ["anna", "anna_ABCDEF", "anna_12345", "anna_1234567", "../x_abcdef",
                                   "a/b_abcdef", "ca", "server", "", None] + [f"x{i}_abcdef" for i in INJECTIONS])
def test_bad_certificate_names(value):
    with pytest.raises(validate.ValidationError):
        validate.cn(value)


@pytest.mark.parametrize("value, expected", [
    ("Example.NET", "example.net"), ("192.168.1.5", "192.168.1.5"), ("[::1]", "::1"),
    (" home.ddns.net ", "home.ddns.net"), ("xn--bcher-kva.example", "xn--bcher-kva.example")])
def test_good_hosts_are_normalized(value, expected):
    assert validate.host(value) == expected


@pytest.mark.parametrize("value", ["", "-a.com", "a..com", "a_b.com", "http://a.com", "a.com/x", "a.com:80",
                                   "x" * 64 + ".com", "a" * 254] + INJECTIONS)
def test_bad_hosts(value):
    with pytest.raises(validate.ValidationError):
        validate.host(value)


def test_networks_and_addresses():
    assert validate.ipv4_networks("192.168.1.7/24, 10.0.0.0/8", "n") == ["192.168.1.0/24", "10.0.0.0/8"]
    assert validate.ipv4_addresses(["1.1.1.1", "9.9.9.9"], "d") == ["1.1.1.1", "9.9.9.9"]
    for bad in ("10.0.0.0/33", "999.1.1.1", "1.1.1.1\npush x", "fe80::1", "a"):
        with pytest.raises(validate.ValidationError):
            validate.ipv4_networks(bad, "n")
        with pytest.raises(validate.ValidationError):
            validate.ipv4_addresses(bad, "d")
    with pytest.raises(validate.ValidationError):
        validate.ipv4_addresses(",".join(["1.1.1.1"] * 9), "d", limit=8)


@pytest.mark.parametrize("value", [0, -1, 3651, True, "5", 1.5, None])
def test_bad_numbers(value):
    with pytest.raises(validate.ValidationError):
        validate.int_range(value, "days", 1, 3650)


def test_server_settings_validation_is_strict():
    good = {"dns": ["1.1.1.1"], "routes": ["10.0.0.0/8"], "full_tunnel": False, "client_to_client": True}
    assert settings.validate_server(good)["routes"] == ["10.0.0.0/8"]
    for key, bad in [("dns", ["1.1.1.1\npush x"]), ("routes", ["10.0.0.0/8; x"]), ("full_tunnel", "yes"),
                     ("client_to_client", 1), ("dns", ["a"] * 9)]:
        with pytest.raises(ValueError):
            settings.validate_server({**good, key: bad})
    with pytest.raises(ValueError):
        settings.validate_server({**good, "routes": []})   # split tunnel with nothing to route
    with pytest.raises(ValueError):
        settings.validate_server({"dns": [], "routes": []})  # missing keys
