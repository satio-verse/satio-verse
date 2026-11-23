"""AES-128-ECB (PKCS7 填充) 加密, 纯 Python: 微信 ClawBot 发图要先把图用 AES-128-ECB 加密再传到微信的 CDN (腾讯官方插件
@tencent-weixin/openclaw-weixin 的做法, 见 weixin.py 的 send_image). 只加密、不解密, 只处理几十 KB 的小图 (二维码); 这台电脑没装
cryptography / pycryptodome, 不为这一处加依赖. 对过 FIPS-197 和 NIST SP 800-38A 的标准向量 (tests/test_weixin_image.py).
"""
from __future__ import annotations


def _tables() -> tuple[list[int], list[int]]:
    # Rijndael 的 S 盒: 照标准的构造 (GF(2^8) 里取逆再做仿射变换) 现算, 不抄一大张表
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ (p << 1) ^ (0x11B if p & 0x80 else 0)) & 0xFF   # p *= 3
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        q ^= 0x09 if q & 0x80 else 0                              # q /= 3
        rot = lambda x, n: ((x << n) | (x >> (8 - n))) & 0xFF
        sbox[p] = q ^ rot(q, 1) ^ rot(q, 2) ^ rot(q, 3) ^ rot(q, 4) ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    xtime = [((x << 1) ^ (0x11B if x & 0x80 else 0)) & 0xFF for x in range(256)]
    return sbox, xtime


_SBOX, _XT = _tables()
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _expand(key: bytes) -> list[list[int]]:
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(11)]


def _block(state: list[int], rks: list[list[int]]) -> bytes:
    s = [a ^ b for a, b in zip(state, rks[0])]
    for rnd in range(1, 11):
        s = [_SBOX[b] for b in s]
        s = [s[(r + 4 * ((c + r) % 4))] for c in range(4) for r in range(4)]  # ShiftRows (按列排的 16 字节)
        if rnd < 10:
            out = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c:4 * c + 4]
                out += [_XT[a0] ^ _XT[a1] ^ a1 ^ a2 ^ a3, a0 ^ _XT[a1] ^ _XT[a2] ^ a2 ^ a3,
                        a0 ^ a1 ^ _XT[a2] ^ _XT[a3] ^ a3, _XT[a0] ^ a0 ^ a1 ^ a2 ^ _XT[a3]]
            s = out
        s = [a ^ b for a, b in zip(s, rks[rnd])]
    return bytes(s)


def padded_size(n: int) -> int:
    """PKCS7 填充后的长度 (正好是 16 的倍数时也要再补一整块)."""
    return (n // 16 + 1) * 16


def encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-ECB + PKCS7. key 必须是 16 字节."""
    if len(key) != 16:
        raise ValueError("AES-128 的密钥要 16 字节")
    rks = _expand(bytes(key))
    pad = 16 - len(data) % 16
    data = bytes(data) + bytes([pad]) * pad
    return b"".join(_block(list(data[i:i + 16]), rks) for i in range(0, len(data), 16))
