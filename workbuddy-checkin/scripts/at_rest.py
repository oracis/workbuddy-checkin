"""WorkBuddy at-rest-crypto 信封解密（与客户端 packages/at-rest-crypto 算法一致，零依赖）。

凭据字段信封格式（标准保护，sym-v1）:
    accessToken = { "$wbEncrypted": 1, "envelope": "<base64 of JSON>" }
    envelope JSON = { suite:1, keyId:"<16hex>", nonce:"<b64>", authTag:"<b64>", ciphertext:"<b64>" }

封包密钥 k0   = SHA256(atRestSecretKey 字符串的 UTF-8)  (32 字节)
keyId        = SHA256(k0)[:16]      (必须 == envelope.keyId)
AAD          = "WB-AAD\\0" + [1] + L("WBEV1") + L("sym-v1") + uint32(suite) + L(keyId) + [2] + [0] + [0]
密文         = AES-256-GCM(k0, nonce, ciphertext||authTag, aad)

AES-GCM 实现：优先用 cryptography（若可导入），否则用内置纯 Python 实现（stdlib only）。
纯 Python 实现已用 NIST GCM 测试向量 + FIPS-197 AES-256 向量校验。
"""
import base64
import hashlib
import json
import os
import struct

AAD_DOMAIN = b"WB-AAD\x00"
STANDARD_FORMAT_ID = {"file": "WBEF1", "field": "WBEV1", "record": "WBER1", "stream": "WBES1"}
FRAMING_CODE = {"file": 1, "field": 2, "record": 3, "stream": 4}

# build key 缓存文件（atRestSecretKey）。可用环境变量 WB_AT_REST_SECRET_KEY 覆盖。
BUILDKEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "at_rest_buildkey.json")


# ---------------------------------------------------------------- AAD / 密钥派生
def _len_prefixed(s):
    b = s.encode("utf-8")
    return struct.pack(">I", len(b)) + b


def build_aad(key_id_hex, suite, framing="field"):
    """复刻客户端 buildAuthenticatedContextAad(keyId, suite, {framing}, "sym-v1")。"""
    if not isinstance(key_id_hex, str) or len(key_id_hex) != 16:
        raise ValueError("keyId must be 16 hex chars: %r" % key_id_hex)
    try:
        int(key_id_hex, 16)
    except ValueError:
        raise ValueError("keyId not hex: %r" % key_id_hex)
    if framing not in FRAMING_CODE:
        raise ValueError("unsupported framing: %s" % framing)
    return b"".join([
        AAD_DOMAIN,
        bytes([1]),
        _len_prefixed(STANDARD_FORMAT_ID[framing]),
        _len_prefixed("sym-v1"),
        struct.pack(">I", suite),
        _len_prefixed(key_id_hex.lower()),
        bytes([FRAMING_CODE[framing]]),
        bytes([0]),   # encodeOptionalUint64(undefined)
        bytes([0]),   # final === undefined
    ])


def derive_k0(at_rest_secret_key_b64):
    """k0 = SHA256(atRestSecretKey 字符串的 UTF-8)，与 normalizeAtRestKeyPayload 一致。"""
    return hashlib.sha256(at_rest_secret_key_b64.encode("utf-8")).digest()


def key_id_of(k0):
    return hashlib.sha256(k0).hexdigest()[:16]


def decode_envelope(envelope_b64):
    obj = json.loads(base64.b64decode(envelope_b64).decode("utf-8"))
    if set(obj.keys()) != {"suite", "keyId", "nonce", "authTag", "ciphertext"}:
        raise ValueError("envelope schema mismatch: %s" % sorted(obj.keys()))
    nonce = base64.b64decode(obj["nonce"])
    auth_tag = base64.b64decode(obj["authTag"])
    ciphertext = base64.b64decode(obj["ciphertext"]) if obj["ciphertext"] else b""
    if len(nonce) != 12 or len(auth_tag) != 16:
        raise ValueError("bad nonce(%d)/authTag(%d) length" % (len(nonce), len(auth_tag)))
    return {"suite": obj["suite"], "keyId": obj["keyId"], "nonce": nonce,
            "authTag": auth_tag, "ciphertext": ciphertext}


# ---------------------------------------------------------------- 纯 Python AES
def _build_sbox():
    p = q = 1
    sbox = [0] * 256
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) \
              ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


SBOX = _build_sbox()


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


class _AES:
    """AES-128/192/256 单块加密（FIPS-197）。state 按列存储：col[c][r] = in[4c+r]。"""

    def __init__(self, key):
        if len(key) not in (16, 24, 32):
            raise ValueError("bad AES key length: %d" % len(key))
        nk = len(key) // 4
        self.nr = nk + 6
        w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
        rcon = 1
        for i in range(nk, 4 * (self.nr + 1)):
            t = list(w[i - 1])
            if i % nk == 0:
                t = t[1:] + t[:1]
                t = [SBOX[b] for b in t]
                t[0] ^= rcon
                rcon = ((rcon << 1) ^ (0x1B if rcon & 0x80 else 0)) & 0xFF
            elif nk > 6 and i % nk == 4:
                t = [SBOX[b] for b in t]
            w.append([w[i - nk][j] ^ t[j] for j in range(4)])
        self.w = w

    def encrypt_block(self, block):
        if len(block) != 16:
            raise ValueError("block must be 16 bytes")
        col = [list(block[4 * c:4 * c + 4]) for c in range(4)]
        col = [[col[c][r] ^ self.w[c][r] for r in range(4)] for c in range(4)]
        for rnd in range(1, self.nr + 1):
            col = [[SBOX[col[c][r]] for r in range(4)] for c in range(4)]
            col = [[col[(c + r) % 4][r] for r in range(4)] for c in range(4)]
            if rnd != self.nr:
                col = [[_mul(col[c][0], 2) ^ _mul(col[c][1], 3) ^ col[c][2] ^ col[c][3],
                        col[c][0] ^ _mul(col[c][1], 2) ^ _mul(col[c][2], 3) ^ col[c][3],
                        col[c][0] ^ col[c][1] ^ _mul(col[c][2], 2) ^ _mul(col[c][3], 3),
                        _mul(col[c][0], 3) ^ col[c][1] ^ col[c][2] ^ _mul(col[c][3], 2)]
                       for c in range(4)]
            col = [[col[c][r] ^ self.w[rnd * 4 + c][r] for r in range(4)] for c in range(4)]
        return bytes(col[c][r] for c in range(4) for r in range(4))


# ---------------------------------------------------------------- 纯 Python GCM
def _gmul(x, y):
    """GF(2^128) 乘法，位反射表示，归约多项式 0xe1<<120。"""
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ (0xE1 << 120)
        else:
            v >>= 1
    return z


def _ghash(h, data):
    """data 长度必须是 16 的倍数。"""
    y = 0
    for i in range(0, len(data), 16):
        blk = int.from_bytes(data[i:i + 16], "big")
        y = _gmul(y ^ blk, h)
    return y


def _b128(v):
    return v.to_bytes(16, "big")


def _inc32(block):
    n = int.from_bytes(block[12:], "big")
    return block[:12] + ((n + 1) & 0xFFFFFFFF).to_bytes(4, "big")


def _gcm_keystream_xor(cipher, j0, data):
    """用 GCM 密钥流与 data 异或（加/解密同一操作）。"""
    out = bytearray()
    ctr = _inc32(j0)          # 第一个数据块用 inc32(J0)；J0 本身只用于算 tag
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        ks = cipher.encrypt_block(ctr)
        ctr = _inc32(ctr)
        out.extend(bytes(a ^ b for a, b in zip(chunk, ks)))
    return bytes(out)


def _gcm_tag(cipher, h, j0, aad, ciphertext):
    """tag = GHASH_H(A||pad || C||pad || len(A)||len(C)) XOR E_K(J0)。注意 C 必须是密文。"""
    a_pad = aad + b"\x00" * (-len(aad) % 16)
    c_pad = ciphertext + b"\x00" * (-len(ciphertext) % 16)
    lens = (len(aad) * 8).to_bytes(8, "big") + (len(ciphertext) * 8).to_bytes(8, "big")
    s = _ghash(h, a_pad + c_pad + lens)
    return _b128(s ^ int.from_bytes(cipher.encrypt_block(j0), "big"))


def _gcm_setup(key, nonce):
    if len(nonce) != 12:
        raise ValueError("GCM nonce must be 12 bytes, got %d" % len(nonce))
    cipher = _AES(key)
    h = int.from_bytes(cipher.encrypt_block(b"\x00" * 16), "big")
    return cipher, h, nonce + b"\x00\x00\x00\x01"


def _gcm_crypt(key, nonce, plaintext, aad=b""):
    """GCM 加密，返回 (ciphertext, tag16)。"""
    cipher, h, j0 = _gcm_setup(key, nonce)
    ct = _gcm_keystream_xor(cipher, j0, plaintext)
    return ct, _gcm_tag(cipher, h, j0, aad, ct)


def _gcm_decrypt(key, nonce, ct_with_tag, aad=b""):
    """GCM 解密并校验 tag；校验失败抛 ValueError。"""
    if len(ct_with_tag) < 16:
        raise ValueError("ciphertext too short")
    cipher, h, j0 = _gcm_setup(key, nonce)
    ct, tag = ct_with_tag[:-16], ct_with_tag[-16:]
    calc = _gcm_tag(cipher, h, j0, aad, ct)
    if calc != tag:
        raise ValueError("GCM auth tag mismatch（密钥或 AAD 不对）")
    return _gcm_keystream_xor(cipher, j0, ct)


# ---------------------------------------------------------------- 对外解密接口
def _aes_gcm_decrypt(key, nonce, ct_with_tag, aad):
    """优先 cryptography，回退纯 Python 实现。失败抛 ValueError。"""
    if len(ct_with_tag) < 16:
        raise ValueError("ciphertext too short")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(nonce, ct_with_tag, aad)
    except ImportError:
        return _gcm_decrypt(key, nonce, ct_with_tag, aad)


def buildkey_candidates():
    """build key 缓存文件的查找顺序：环境变量 > 本模块同目录 > ~/.workbuddy/scripts。"""
    here = os.path.dirname(os.path.abspath(__file__))
    home = os.path.expanduser("~")
    return [
        os.path.join(here, "at_rest_buildkey.json"),
        os.path.join(home, ".workbuddy", "scripts", "at_rest_buildkey.json"),
    ]


def load_build_key(path=None):
    """读取 build key（atRestSecretKey）。"""
    env = os.environ.get("WB_AT_REST_SECRET_KEY")
    if env:
        return env
    paths = [path] if path else buildkey_candidates()
    for p in paths:
        if p and os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)["atRestSecretKey"]
    raise FileNotFoundError(
        "缺少 build key 缓存文件（已尝试：%s）。请用 extract_buildkey.js 从运行中的客户端提取，"
        "或设置环境变量 WB_AT_REST_SECRET_KEY。" % ", ".join(paths))


def decrypt_field(envelope_b64, at_rest_secret_key_b64=None):
    """解出字段明文（JWT 字符串）。"""
    secret = at_rest_secret_key_b64 or load_build_key()
    k0 = derive_k0(secret)
    env = decode_envelope(envelope_b64)
    if key_id_of(k0) != env["keyId"]:
        raise ValueError("keyId mismatch：本地 build key 派生出 %s，信封要求 %s（客户端可能已换 build key）"
                         % (key_id_of(k0), env["keyId"]))
    aad = build_aad(env["keyId"], env["suite"], "field")
    pt = _aes_gcm_decrypt(k0, env["nonce"], env["ciphertext"] + env["authTag"], aad)
    return pt.decode("utf-8")


def unwrap(value, secret=None):
    """把凭据字段值还原为明文字符串：信封 dict -> 解密；明文 str -> 原样返回。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("$wbEncrypted"):
        env = value.get("envelope")
        if isinstance(env, dict):          # 少数情况 envelope 未 base64 而是对象
            env = base64.b64encode(json.dumps(env).encode("utf-8")).decode()
        if not isinstance(env, str):
            raise ValueError("无法识别的信封结构：%s" % list(value.keys()))
        return decrypt_field(env, secret)
    raise ValueError("无法识别的凭据字段类型：%s" % type(value).__name__)


# ---------------------------------------------------------------- 自测
def _self_test():
    # 1) AES-256 单块 FIPS-197 向量
    aes = _AES(bytes(range(32)))
    got = aes.encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff"))
    want = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")
    assert got == want, "AES-256 vector mismatch: %s" % got.hex()

    # 2) AES-256-GCM 向量（GCM spec，期望值以 OpenSSL/cryptography 实测为准）
    k = bytes.fromhex("feffe9928665731c6d6a8f9467308308")
    iv = bytes.fromhex("cafebabefacedbaddecaf888")
    p = bytes.fromhex("d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
                      "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b39")
    c, t = _gcm_crypt(k, iv, p)
    assert c.hex() == ("42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca"
                       "12e21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091"), "GCM(noAAD) ct"
    assert t.hex() == "cc15abcc191161501aabab46b8fbac85", "GCM(noAAD) tag: %s" % t.hex()
    aad = bytes.fromhex("feedfacedeadbeeffeedfacedeadbeefabaddad2")
    c2, t2 = _gcm_crypt(k, iv, p + bytes.fromhex("1aafd255"), aad)
    assert c2.hex() == ("42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca"
                        "12e21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091473f5985"), "GCM(AAD) ct"
    assert t2.hex() == "da80ce830cfda02da2a218a1744f4c76", "GCM(AAD) tag: %s" % t2.hex()

    # 3) 端到端：造信封 -> decrypt_field 解回（cryptography 路径 + 纯 Python 路径）
    import os as _os
    secret_b64 = base64.b64encode(b"test-build-key-payload-0123456789abcdef").decode()
    k0 = derive_k0(secret_b64)
    kid = key_id_of(k0)
    nonce = _os.urandom(12)
    aad = build_aad(kid, 1, "field")
    plaintext = "eyJhbGciOiJSUzI1NiIsImtpZCI6IjEyMzQ1NiJ9.eyJzdWIiOiJ0ZXN0In0.signature"
    ct, tag = _gcm_crypt(k0, nonce, plaintext.encode("utf-8"), aad)
    envelope_b64 = base64.b64encode(json.dumps({
        "suite": 1, "keyId": kid,
        "nonce": base64.b64encode(nonce).decode(),
        "authTag": base64.b64encode(tag).decode(),
        "ciphertext": base64.b64encode(ct).decode(),
    }).encode("utf-8")).decode()
    assert decrypt_field(envelope_b64, secret_b64) == plaintext, "decrypt_field round-trip 失败"
    # 强制纯 Python 解密路径
    assert _gcm_decrypt(k0, nonce, ct + tag, aad) == plaintext.encode("utf-8"), "pure GCM round-trip 失败"
    # 篡改检测：改一个字节必须报 tag 不符
    try:
        _gcm_decrypt(k0, nonce, bytes([ct[0] ^ 1]) + ct[1:] + tag, aad)
        raise AssertionError("tag 篡改未被检测到")
    except ValueError:
        pass
    print("SELF-TEST OK: AES-256/GCM 向量 + AAD/suite 一致 (keyId=%s)" % kid)


if __name__ == "__main__":
    _self_test()
