#!/usr/bin/env python3
"""
从**正在运行的** Chrome 中提取 weread.qq.com 的 cookie，生成 Playwright storage_state.json。

为什么需要这个（实测结论）：
    微信读书的付费/试读权限绑定在**具体会话**上。
    用 Playwright 扫码重新登录（同一账号、同一头像），拿到的会话只有 1 页试读，
    会卡在「版权信息」页并弹付费墙；而用户自己 Chrome 里的会话能读全书。
    → 直接复用用户 Chrome 里那个**有权限的会话**，无需关闭 Chrome、无需重新扫码。

macOS Chrome cookie 加密：
    - master key 来自 Keychain 的 "Chrome Safe Storage"
    - PBKDF2-HMAC-SHA1(password=safe_storage_key, salt=b'saltysalt', 1003 轮) → 16 字节 AES key
    - value 前缀 v10/v11 → AES-128-CBC，IV = 16 个空格；prefix_len=3
    - value 前缀 v20 → App-Bound Encryption（仅 Windows），macOS 上不会出现
    - 明文值（无前缀）直接用

用法：
    python3 capture/extract_chrome_cookies.py [--profile Default] [--out state/storage_state.json]
"""

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME_ROOT = Path("~/Library/Application Support/Google/Chrome").expanduser()
DOMAIN = "weread.qq.com"


def safe_storage_key() -> bytes:
    """从 Keychain 取 'Chrome Safe Storage' 密码。

    ⚠️ 两个实测坑：
    1. Chrome 的 keychain 项是 svce="Chrome Safe Storage" / acct="Chrome"，
       用 `-a "Chrome Safe Storage"` 会报 "item could not be found"。
    2. PBKDF2 的 password 要用 **base64 字符串的原始 ASCII 字节**，
       不是 base64 解码后的字节（用解码值会解出乱码，已实测）。
    """
    for args in (
        ["-s", "Chrome Safe Storage", "-a", "Chrome", "-w"],
        ["-s", "Chrome Safe Storage", "-w"],
    ):
        out = subprocess.run(["security", "find-generic-password", *args],
                             capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().encode("utf-8")
    raise RuntimeError(
        "无法读取 Keychain 'Chrome Safe Storage'。\n"
        "  排查：security find-generic-password -s \"Chrome Safe Storage\" -a Chrome -w\n"
        f"  stderr: {out.stderr.strip()}")



def decrypt_value(enc: bytes, key: bytes, host_key: str = "") -> str | None:
    """解密 Chrome cookie。

    ⚠️ Chrome 的 encrypted_value 明文前面带 **32 字节的 SHA256(host_key)** 前缀
       （domain-bound cookie），不去掉会解出乱码。
    """
    if not enc:
        return ""
    if enc[:3] not in (b"v10", b"v11"):
        # 无前缀 = 明文
        return enc.decode("utf-8", errors="replace")
    if enc[:3] == b"v20":
        return None  # App-Bound Encryption，macOS 上不会出现
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16,
        salt=b"saltysalt", iterations=1003,
    )
    aes_key = kdf.derive(key)
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(b" " * 16))
    dec = cipher.decryptor()
    plain = dec.update(enc[3:]) + dec.finalize()
    # 去 PKCS7 padding
    pad = plain[-1]
    if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
        plain = plain[:-pad]
    # 去 32 字节 host 哈希前缀
    if host_key:
        import hashlib
        h = hashlib.sha256(host_key.encode("utf-8")).digest()
        if plain[:32] == h:
            plain = plain[32:]
    return plain.decode("utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="Default")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "state" / "storage_state.json"))
    ap.add_argument("--also-cookie-txt", default=str(Path(__file__).resolve().parent.parent / "state" / "cookie.txt"))
    args = ap.parse_args()

    db = CHROME_ROOT / args.profile / "Cookies"
    if not db.exists():
        print(f"❌ 找不到 {db}", file=sys.stderr)
        return 1

    # 复制一份，避免与运行中的 Chrome 抢锁
    tmpdir = tempfile.mkdtemp(prefix="chrome_cookies_")
    tmp_db = str(Path(tmpdir) / "Cookies")
    shutil.copy2(db, tmp_db)

    key = safe_storage_key()
    con = sqlite3.connect(tmp_db)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT name, value, encrypted_value, host_key, path, expires_utc, "
        "is_secure, is_httponly, samesite "
        "FROM cookies WHERE host_key LIKE ?",
        (f"%{DOMAIN}%",),
    ).fetchall()
    con.close()

    if not rows:
        print(f"⚠️  Chrome 里没有 {DOMAIN} 的 cookie（该域名没登录过？）")
        return 1

    cookies = []
    skipped = []
    for name, enc_val, blob, host, cpath, exp_utc, secure, httponly, samesite in rows:
        raw = blob if blob else (enc_val or b"").encode("utf-8", errors="replace")
        # 未加密的列存在时优先用它
        val = "" if enc_val is None and blob is None else decrypt_value(raw, key, host)
        if val is None:
            skipped.append(name)
            continue
        # Chrome 的 expires_utc 是 1601-01-01 起的 100ns 计数
        expires = -1
        if exp_utc:
            import datetime
            epoch = datetime.datetime(1601, 1, 1, tzinfo=datetime.UTC)
            ts = epoch + datetime.timedelta(microseconds=exp_utc / 10)
            expires = int(ts.timestamp())

        cookie = {
            "name": name, "value": val, "domain": host, "path": cpath or "/",
            "httpOnly": bool(httponly), "secure": bool(secure),
            "sameSite": {"0": "Strict", "1": "Lax", "2": "None"}.get(str(samesite), "Lax"),
        }
        # Playwright 只接受 -1 或正数；会话 cookie（无过期时间）直接省略该字段
        if expires > 0:
            cookie["expires"] = expires
        cookies.append(cookie)

    print(f"▶ 从 Chrome[{args.profile}] 提取到 {len(cookies)} 条 {DOMAIN} cookie")
    for c in cookies:
        v = c["value"]
        print(f"   {c['name']:<14} len={len(v):<4} {v[:24]}")
    if skipped:
        print(f"   ⚠️ 无法解密(App-Bound): {skipped}")

    state = {"cookies": cookies, "origins": []}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"\n✅ 已写入 {out}")

    # 同时导出 Netscape 格式（兼容老代码）
    lines = [
        "# Netscape HTTP Cookie File (extended with httpOnly as 8th column)",
        "# Generated by capture/extract_chrome_cookies.py — do not edit manually.",
        "#", ]
    for c in cookies:
        lines.append("\t".join([
            c["domain"], "TRUE" if c["domain"].startswith(".") else "FALSE",
            c["path"], "TRUE" if c["secure"] else "FALSE",
            str(c.get("expires", 0)) if c.get("expires", 0) > 0 else "0",
            c["name"], c["value"], "TRUE" if c["httpOnly"] else "FALSE",
        ]))
    Path(args.also_cookie_txt).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✅ 已写入 {args.also_cookie_txt}")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
