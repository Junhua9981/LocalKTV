"""Two ciphers for QQ's cloud-QRC blobs.

1. `qq_des3` / `qq_decrypt_hex` — QQ's client cipher (primary path).  The
   transcription follows qqmusic_crypto.go from ibratabian17/lyricsplus
   (Apache-2.0), an independent reimplementation of QQ's client crypto, and
   was cross-checked against the same algorithm in LDDC and
   ESLyric-LyricsSource (both GPL-3.0 — consulted for study, not copied).
   A stock 3DES cannot decrypt these blobs: the client's bit numbering /
   tables differ from FIPS 46 beyond the two well-known S-box cells.

2. `des3_ecb` — an original standard DES/3DES (FIPS 46 construction, kept as
   a fallback if the service ever switches and for tests, where it is
   cross-validated bit-for-bit against pycryptodome).

`server/lyrics_test.py` round-trips both paths and, when the plug-in's test
data is present, also decrypts a real blob end to end.
"""

from __future__ import annotations

import zlib

# --- standard FIPS 46 tables (bit positions are 1-based, MSB first) ---------

_IP = (
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
)
_FP = (
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
)
_E = (
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13,
    12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21, 22, 23,
    24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
)
_P = (
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
    2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25,
)
_PC1 = (
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
    10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
    14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4,
)
_PC2 = (
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10,
    23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
    41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
)
_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)

# --- S-boxes ---------------------------------------------------------------
# FIPS 46 S-boxes, row-major (row = b5b0, col = b4b3b2b1 of the 6-bit input).
_SBOX = (
    (
        14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
        0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
        4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
        15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13,
    ),
    (
        15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
        3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
        0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
        13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9,
    ),
    (
        10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
        13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
        13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
        1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12,
    ),
    (
        7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
        13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
        10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
        3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14,
    ),
    (
        2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
        14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
        4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
        11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3,
    ),
    (
        12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
        10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
        9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
        4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13,
    ),
    (
        4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
        13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
        1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
        6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12,
    ),
    (
        13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
        1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
        7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
        2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11,
    ),
)

def _permute(value: int, table: tuple[int, ...], src_bits: int) -> int:
    """Gather bits (1-based, MSB = position 1) into a new integer."""
    out = 0
    for pos in table:
        out = (out << 1) | ((value >> (src_bits - pos)) & 1)
    return out


def _key_schedule(key64: int) -> list[int]:
    """16 round keys for one DES key (encryption order)."""
    permuted = _permute(key64, _PC1, 64)
    c = (permuted >> 28) & 0xFFFFFFF
    d = permuted & 0xFFFFFFF
    keys: list[int] = []
    for shift in _SHIFTS:
        c = ((c << shift) | (c >> (28 - shift))) & 0xFFFFFFF
        d = ((d << shift) | (d >> (28 - shift))) & 0xFFFFFFF
        keys.append(_permute((c << 28) | d, _PC2, 56))
    return keys


def _round_function(state32: int, subkey48: int, sbox: tuple[tuple[int, ...], ...]) -> int:
    expanded = _permute(state32, _E, 32) ^ subkey48
    out = 0
    for box in range(8):
        six = (expanded >> (42 - 6 * box)) & 0x3F
        row = ((six >> 4) & 0x2) | (six & 0x1)
        col = (six >> 1) & 0xF
        out = (out << 4) | sbox[box][row * 16 + col]
    return _permute(out, _P, 32)


def _des_block(block64: int, keys: list[int], sbox: tuple[tuple[int, ...], ...]) -> int:
    state = _permute(block64, _IP, 64)
    left, right = state >> 32, state & 0xFFFFFFFF
    for subkey in keys:
        left, right = right, left ^ _round_function(right, subkey, sbox)
    return _permute((right << 32) | left, _FP, 64)


def des_ecb(
    key8: bytes,
    data: bytes,
    *,
    decrypt: bool = False,
) -> bytes:
    """Single DES in ECB mode over a whole byte string (length % 8 == 0)."""
    if len(key8) != 8:
        raise ValueError("DES key must be 8 bytes")
    if len(data) % 8:
        raise ValueError("data length must be a multiple of 8")
    keys = _key_schedule(int.from_bytes(key8, "big"))
    if decrypt:
        keys = keys[::-1]
    out = bytearray()
    for off in range(0, len(data), 8):
        block = int.from_bytes(data[off:off + 8], "big")
        out += _des_block(block, keys, _SBOX).to_bytes(8, "big")
    return bytes(out)


def des3_ecb(
    key24: bytes,
    data: bytes,
    *,
    decrypt: bool = False,
) -> bytes:
    """3DES-EDE3 in ECB mode (encrypt: E_K3∘D_K2∘E_K1 — pycryptodome layout)."""
    if len(key24) != 24:
        raise ValueError("3DES key must be 24 bytes")
    k1, k2, k3 = key24[:8], key24[8:16], key24[16:24]
    if not decrypt:
        return des_ecb(k3, des_ecb(k2, des_ecb(k1, data), decrypt=True))
    return des_ecb(k1, des_ecb(k2, des_ecb(k3, data, decrypt=True)), decrypt=True)


# ---------------------------------------------------------------------------
# QQ's client cipher — primary path for cloud QRC
#
# Transcribed from qqmusic_crypto.go (ibratabian17/lyricsplus, Apache-2.0);
# cross-checked against the same algorithm in LDDC / ESLyric-LyricsSource
# (GPL-3.0 — consulted for study only, not copied).  Verified end-to-end
# against a real GetPlayLyricInfo blob (kept with the plug-in's test data).
# ---------------------------------------------------------------------------

QQ_QRC_KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"

_QQ_SBOX = (
    (
        14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
        0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
        4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
        15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13,
    ),
    (
        15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
        3, 13, 4, 7, 15, 2, 8, 15, 12, 0, 1, 10, 6, 9, 11, 5,
        0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
        13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9,
    ),
    (
        10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
        13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
        13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
        1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12,
    ),
    (
        7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
        13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
        10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
        3, 15, 0, 6, 10, 10, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14,
    ),
    (
        2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
        14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
        4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
        11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3,
    ),
    (
        12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
        10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
        9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
        4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13,
    ),
    (
        4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
        13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
        1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
        6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12,
    ),
    (
        13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
        1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
        7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
        2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11,
    ),
)

_QQ_KEY_RND_SHIFT = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_QQ_KEY_PERM_C = (
    56, 48, 40, 32, 24, 16, 8, 0, 57, 49, 41, 33, 25, 17, 9, 1,
    58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35,
)
_QQ_KEY_PERM_D = (
    62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5,
    60, 52, 44, 36, 28, 20, 12, 4, 27, 19, 11, 3,
)
_QQ_KEY_COMPRESSION = (
    13, 16, 10, 23, 0, 4, 2, 27, 14, 5, 20, 9, 22, 18, 11, 3,
    25, 7, 15, 6, 26, 19, 12, 1, 40, 51, 30, 36, 46, 54, 29, 39,
    50, 44, 32, 47, 43, 48, 38, 55, 33, 52, 45, 41, 49, 35, 28, 31,
)
_QQ_LEFT = (
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
)
_QQ_RIGHT = (
    56, 48, 40, 32, 24, 16, 8, 0, 58, 50, 42, 34, 26, 18, 10, 2,
    60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
)

_QQ_ENCRYPT, _QQ_DECRYPT = 1, 0


def _qq_bitnum(data: bytes, pos: int, shift: int) -> int:
    byte_idx = (pos // 32) * 4 + 3 - ((pos % 32) // 8)
    return ((data[byte_idx] >> (7 - (pos % 8))) & 1) << shift


def _qq_bitnum_intr(value: int, pos: int, shift: int) -> int:
    return ((value >> (31 - pos)) & 1) << shift


def _qq_bitnum_intl(value: int, pos: int, shift: int) -> int:
    return ((value << pos) & 0x80000000) >> shift


def _qq_sbox_bit(value: int) -> int:
    return (value & 32) | ((value & 31) >> 1) | ((value & 1) << 4)


def _qq_initial_permutation(block: bytes) -> tuple[int, int]:
    left = right = 0
    for i in range(32):
        left |= _qq_bitnum(block, _QQ_LEFT[i], 31 - i)
        right |= _qq_bitnum(block, _QQ_RIGHT[i], 31 - i)
    return left, right


def _qq_inverse_permutation(s0: int, s1: int) -> bytes:
    out = [0] * 8
    # byte order 3,2,1,0,7,6,5,4; each byte = s1/s0 bits a, a+8, a+16, a+24
    for byte_idx, a in ((3, 7), (2, 6), (1, 5), (0, 4), (7, 3), (6, 2), (5, 1), (4, 0)):
        out[byte_idx] = (
            _qq_bitnum_intr(s1, a, 7) | _qq_bitnum_intr(s0, a, 6)
            | _qq_bitnum_intr(s1, a + 8, 5) | _qq_bitnum_intr(s0, a + 8, 4)
            | _qq_bitnum_intr(s1, a + 16, 3) | _qq_bitnum_intr(s0, a + 16, 2)
            | _qq_bitnum_intr(s1, a + 24, 1) | _qq_bitnum_intr(s0, a + 24, 0)
        )
    return bytes(out)


def _qq_f(state: int, key: list[int]) -> int:
    t1 = (_qq_bitnum_intl(state, 31, 0) | ((state & 0xF0000000) >> 1) | _qq_bitnum_intl(state, 4, 5) |
          _qq_bitnum_intl(state, 3, 6) | ((state & 0x0F000000) >> 3) | _qq_bitnum_intl(state, 8, 11) |
          _qq_bitnum_intl(state, 7, 12) | ((state & 0x00F00000) >> 5) | _qq_bitnum_intl(state, 12, 17) |
          _qq_bitnum_intl(state, 11, 18) | ((state & 0x000F0000) >> 7) | _qq_bitnum_intl(state, 16, 23))
    t2 = (_qq_bitnum_intl(state, 15, 0) | ((state & 0x0000F000) << 15) | _qq_bitnum_intl(state, 20, 5) |
          _qq_bitnum_intl(state, 19, 6) | ((state & 0x00000F00) << 13) | _qq_bitnum_intl(state, 24, 11) |
          _qq_bitnum_intl(state, 23, 12) | ((state & 0x000000F0) << 11) | _qq_bitnum_intl(state, 28, 17) |
          _qq_bitnum_intl(state, 27, 18) | ((state & 0x0000000F) << 9) | _qq_bitnum_intl(state, 0, 23))
    lrg = (
        ((t1 >> 24) & 0xFF) ^ key[0],
        ((t1 >> 16) & 0xFF) ^ key[1],
        ((t1 >> 8) & 0xFF) ^ key[2],
        ((t2 >> 24) & 0xFF) ^ key[3],
        ((t2 >> 16) & 0xFF) ^ key[4],
        ((t2 >> 8) & 0xFF) ^ key[5],
    )
    mixed = (
        (_QQ_SBOX[0][_qq_sbox_bit(lrg[0] >> 2)] << 28)
        | (_QQ_SBOX[1][_qq_sbox_bit(((lrg[0] & 0x03) << 4) | (lrg[1] >> 4))] << 24)
        | (_QQ_SBOX[2][_qq_sbox_bit(((lrg[1] & 0x0F) << 2) | (lrg[2] >> 6))] << 20)
        | (_QQ_SBOX[3][_qq_sbox_bit(lrg[2] & 0x3F)] << 16)
        | (_QQ_SBOX[4][_qq_sbox_bit(lrg[3] >> 2)] << 12)
        | (_QQ_SBOX[5][_qq_sbox_bit(((lrg[3] & 0x03) << 4) | (lrg[4] >> 4))] << 8)
        | (_QQ_SBOX[6][_qq_sbox_bit(((lrg[4] & 0x0F) << 2) | (lrg[5] >> 6))] << 4)
        | _QQ_SBOX[7][_qq_sbox_bit(lrg[5] & 0x3F)]
    )
    return (
        _qq_bitnum_intl(mixed, 15, 0) | _qq_bitnum_intl(mixed, 6, 1) | _qq_bitnum_intl(mixed, 19, 2)
        | _qq_bitnum_intl(mixed, 20, 3) | _qq_bitnum_intl(mixed, 28, 4) | _qq_bitnum_intl(mixed, 11, 5)
        | _qq_bitnum_intl(mixed, 27, 6) | _qq_bitnum_intl(mixed, 16, 7) | _qq_bitnum_intl(mixed, 0, 8)
        | _qq_bitnum_intl(mixed, 14, 9) | _qq_bitnum_intl(mixed, 22, 10) | _qq_bitnum_intl(mixed, 25, 11)
        | _qq_bitnum_intl(mixed, 4, 12) | _qq_bitnum_intl(mixed, 17, 13) | _qq_bitnum_intl(mixed, 30, 14)
        | _qq_bitnum_intl(mixed, 9, 15) | _qq_bitnum_intl(mixed, 1, 16) | _qq_bitnum_intl(mixed, 7, 17)
        | _qq_bitnum_intl(mixed, 23, 18) | _qq_bitnum_intl(mixed, 13, 19) | _qq_bitnum_intl(mixed, 31, 20)
        | _qq_bitnum_intl(mixed, 26, 21) | _qq_bitnum_intl(mixed, 2, 22) | _qq_bitnum_intl(mixed, 8, 23)
        | _qq_bitnum_intl(mixed, 18, 24) | _qq_bitnum_intl(mixed, 12, 25) | _qq_bitnum_intl(mixed, 29, 26)
        | _qq_bitnum_intl(mixed, 5, 27) | _qq_bitnum_intl(mixed, 21, 28) | _qq_bitnum_intl(mixed, 10, 29)
        | _qq_bitnum_intl(mixed, 3, 30) | _qq_bitnum_intl(mixed, 24, 31)
    )


def _qq_crypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    s0, s1 = _qq_initial_permutation(block)
    for i in range(15):
        s0, s1 = s1, _qq_f(s1, round_keys[i]) ^ s0
    s0 = _qq_f(s1, round_keys[15]) ^ s0
    return _qq_inverse_permutation(s0, s1)


def _qq_key_schedule(key8: bytes, mode: int) -> list[list[int]]:
    schedule = [[0] * 6 for _ in range(16)]
    c = d = 0
    for i in range(28):
        c |= _qq_bitnum(key8, _QQ_KEY_PERM_C[i], 31 - i)
        d |= _qq_bitnum(key8, _QQ_KEY_PERM_D[i], 31 - i)
    for i in range(16):
        shift = _QQ_KEY_RND_SHIFT[i]
        c = ((c << shift) | (c >> (28 - shift))) & 0xFFFFFFF0
        d = ((d << shift) | (d >> (28 - shift))) & 0xFFFFFFF0
        target = 15 - i if mode == _QQ_DECRYPT else i
        for j in range(24):
            schedule[target][j // 8] |= _qq_bitnum_intr(c, _QQ_KEY_COMPRESSION[j], 7 - (j % 8))
        for j in range(24, 48):
            schedule[target][j // 8] |= _qq_bitnum_intr(d, _QQ_KEY_COMPRESSION[j] - 27, 7 - (j % 8))
    return schedule


def _qq_setup(key24: bytes, mode: int) -> list[list[list[int]]]:
    if mode == _QQ_ENCRYPT:
        return [
            _qq_key_schedule(key24[:8], _QQ_ENCRYPT),
            _qq_key_schedule(key24[8:16], _QQ_DECRYPT),
            _qq_key_schedule(key24[16:24], _QQ_ENCRYPT),
        ]
    return [
        _qq_key_schedule(key24[16:24], _QQ_DECRYPT),
        _qq_key_schedule(key24[8:16], _QQ_ENCRYPT),
        _qq_key_schedule(key24[:8], _QQ_DECRYPT),
    ]


def qq_des3_ecb(data: bytes, *, decrypt: bool = False, key: bytes = QQ_QRC_KEY) -> bytes:
    """QQ's 3DES-ECB over a whole byte string (length % 8 == 0)."""
    if len(data) % 8:
        raise ValueError("data length must be a multiple of 8")
    schedule = _qq_setup(key, _QQ_DECRYPT if decrypt else _QQ_ENCRYPT)
    out = bytearray()
    for off in range(0, len(data), 8):
        block = data[off:off + 8]
        for i in range(3):
            block = _qq_crypt_block(block, schedule[i])
        out += block
    return bytes(out)


def qq_decrypt_hex(text: str) -> str:
    """QQ cloud-QRC: hex -> QQ 3DES-ECB -> zlib/raw-deflate -> utf-8.

    Raises ValueError when the payload cannot be decoded.
    """
    text = (text or "").strip()
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError("not hex") from exc
    if not raw or len(raw) % 8:
        raise ValueError(f"invalid ciphertext length: {len(raw)}")
    decrypted = qq_des3_ecb(raw, decrypt=True)
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
        try:
            out = zlib.decompress(decrypted, wbits)
        except Exception:
            continue
        if out:
            return out.decode("utf-8")
    raise ValueError("zlib and raw deflate both failed")
