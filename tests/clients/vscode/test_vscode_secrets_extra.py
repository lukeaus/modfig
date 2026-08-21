def test_oscrypt_cipher_round_trip_handles_full_block_plaintext() -> None:
    from modfig.clients.vscode.secrets import _decrypt, _encrypt

    key = b"0" * 16
    plaintext = b"x" * 16

    assert _decrypt(_encrypt(plaintext, key), key) == plaintext
