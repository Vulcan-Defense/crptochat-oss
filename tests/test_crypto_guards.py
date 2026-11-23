"""Boundary guards that prevent AEAD key/nonce reuse."""

import pytest

from crptochat import crypto


def test_chain_refuses_to_wrap_its_wire_counter():
    chain = crypto.Chain(b"\x01" * 32, crypto.MAX_MESSAGE_INDEX)
    assert chain.step()[0] == crypto.MAX_MESSAGE_INDEX
    # The last representable counter is valid, but the next one would not fit
    # in the protocol's four-byte field and must not silently wrap.
    with pytest.raises(crypto.CryptoError, match="counter exhausted"):
        chain.step()


def test_double_ratchet_refuses_an_exhausted_sending_counter():
    alice_key, bob_key = crypto.ed_pub(crypto.ed_generate()), crypto.ed_pub(crypto.ed_generate())
    alice_dh, bob_dh = crypto.x_generate(), crypto.x_generate()
    ratchet = crypto.DoubleRatchet(b"\x02" * 32, alice_key, bob_key, alice_dh, crypto.x_pub(bob_dh))
    ratchet.ns = crypto.MAX_MESSAGE_INDEX + 1
    with pytest.raises(crypto.CryptoError, match="counter exhausted"):
        ratchet.encrypt(b"never reuse a nonce", b"test|")
