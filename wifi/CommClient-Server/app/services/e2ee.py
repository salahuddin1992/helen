"""
End-to-end encryption — Signal-style Double Ratchet.

Design
------
Helen messages are encrypted client-to-client with no server-side
plaintext access. The server stores only:
  * Public identity keys (for trust on first use)
  * Pre-keys (Curve25519) so an offline recipient can still receive
  * Encrypted message ciphertexts + headers
The session key, message keys, and chain keys never leave the
endpoints.

Wire shape (per direction of conversation)
------------------------------------------
  identity_key  : Ed25519 long-term, rotated only on user request
  signed_pre    : Curve25519 medium-term (rotated weekly)
  one_time_pre  : Curve25519 single-use, replenished on demand
  ratchet_key   : Curve25519 ephemeral, rotates every message

Key derivation
--------------
  X3DH for initial shared secret:
      DH1 = DH(IK_a, SPK_b)
      DH2 = DH(EK_a, IK_b)
      DH3 = DH(EK_a, SPK_b)
      DH4 = DH(EK_a, OPK_b)        (if a one-time pre-key was used)
      SK  = HKDF(DH1 || DH2 || DH3 || DH4)

  Double Ratchet for message keys:
      RK_n, CK_n   = HKDF(DH(rachet_key_old, rachet_key_new), RK_{n-1})
      MK_n, CK_{n+1} = HKDF("ratchet-step", CK_n)

This module is pure-Python on top of ``cryptography`` (already a
hard dep of Helen-Server). It exposes the same primitives the
production Signal lib does, in a form a custom client (Electron /
Web / Mobile) can call directly via JSON over WS.

Security caveats
----------------
* This is not a full Signal Protocol implementation — no group
  ratchet (sender keys), no PNI/MLS. For 1:1 chat it provides the
  same forward-secrecy + post-compromise-security guarantees.
* Implementations are constant-time where feasible. Key material
  zeroed by Python's GC, which is best-effort.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# ── Identity keys ──────────────────────────────────────────────────


@dataclass
class IdentityKey:
    """Long-term identity key — X25519 for DH operations.

    The same key material doubles as the user's stable identity in
    the trust-on-first-use model. We deliberately don't use Ed25519
    here because X3DH needs raw DH on the identity key. A separate
    Ed25519 key exists for signing pre-keys (see ``SigningKey``).
    """
    private: bytes        # raw 32-byte X25519 private
    public: bytes         # raw 32-byte X25519 public

    @classmethod
    def generate(cls) -> "IdentityKey":
        sk = x25519.X25519PrivateKey.generate()
        return cls(
            private=sk.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            public=sk.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
        )


@dataclass
class SigningKey:
    """Ed25519 key purely for signing pre-key bundles."""
    private: bytes
    public: bytes

    @classmethod
    def generate(cls) -> "SigningKey":
        sk = ed25519.Ed25519PrivateKey.generate()
        return cls(
            private=sk.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            public=sk.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
        )

    def sign(self, msg: bytes) -> bytes:
        sk = ed25519.Ed25519PrivateKey.from_private_bytes(self.private)
        return sk.sign(msg)

    @staticmethod
    def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
        try:
            pk = ed25519.Ed25519PublicKey.from_public_bytes(pub)
            pk.verify(sig, msg)
            return True
        except Exception:
            return False


@dataclass
class PreKey:
    """Curve25519 pre-key (signed or one-time)."""
    private: bytes
    public: bytes
    signature: Optional[bytes] = None   # only for SignedPreKey
    pre_key_id: int = 0

    @classmethod
    def generate(cls, pre_key_id: int = 0,
                 signing_key: Optional["SigningKey"] = None
                 ) -> "PreKey":
        sk = x25519.X25519PrivateKey.generate()
        priv = sk.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub = sk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        sig = signing_key.sign(pub) if signing_key else None
        return cls(private=priv, public=pub,
                   signature=sig, pre_key_id=pre_key_id)


# ── X3DH key agreement ─────────────────────────────────────────────


def _dh(priv: bytes, pub: bytes) -> bytes:
    sk = x25519.X25519PrivateKey.from_private_bytes(priv)
    pk = x25519.X25519PublicKey.from_public_bytes(pub)
    return sk.exchange(pk)


def x3dh_initiate(
    *,
    my_identity_priv: bytes,
    my_ephemeral_priv: bytes,
    their_identity_pub: bytes,
    their_signed_pre_pub: bytes,
    their_one_time_pre_pub: Optional[bytes] = None,
) -> bytes:
    """Initiator side of X3DH. Returns the 32-byte shared secret."""
    dh1 = _dh(my_identity_priv, their_signed_pre_pub)
    dh2 = _dh(my_ephemeral_priv, their_identity_pub)
    dh3 = _dh(my_ephemeral_priv, their_signed_pre_pub)
    material = dh1 + dh2 + dh3
    if their_one_time_pre_pub:
        material += _dh(my_ephemeral_priv, their_one_time_pre_pub)
    hk = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"\x00" * 32,
        info=b"helen-x3dh-v1",
    )
    return hk.derive(material)


def x3dh_respond(
    *,
    my_identity_priv: bytes,
    my_signed_pre_priv: bytes,
    their_identity_pub: bytes,
    their_ephemeral_pub: bytes,
    my_one_time_pre_priv: Optional[bytes] = None,
) -> bytes:
    """Responder side of X3DH. Same shared secret as the initiator."""
    dh1 = _dh(my_signed_pre_priv, their_identity_pub)
    dh2 = _dh(my_identity_priv, their_ephemeral_pub)
    dh3 = _dh(my_signed_pre_priv, their_ephemeral_pub)
    material = dh1 + dh2 + dh3
    if my_one_time_pre_priv:
        material += _dh(my_one_time_pre_priv, their_ephemeral_pub)
    hk = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"\x00" * 32,
        info=b"helen-x3dh-v1",
    )
    return hk.derive(material)


# ── Double Ratchet ─────────────────────────────────────────────────


def _kdf_rk(rk: bytes, dh_out: bytes) -> tuple[bytes, bytes]:
    """Root-key step → (new_root_key, new_chain_key)."""
    hk = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=rk,
        info=b"helen-dr-rk",
    )
    out = hk.derive(dh_out)
    return out[:32], out[32:]


def _kdf_ck(ck: bytes) -> tuple[bytes, bytes]:
    """Chain-key step → (new_chain_key, message_key)."""
    hk = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=b"\x00" * 32,
        info=b"helen-dr-ck-step",
    )
    out = hk.derive(ck)
    return out[:32], out[32:]


@dataclass
class RatchetState:
    """Per-conversation Double Ratchet state."""
    root_key: bytes
    sending_chain_key: Optional[bytes] = None
    receiving_chain_key: Optional[bytes] = None
    sending_keypair_priv: Optional[bytes] = None
    sending_keypair_pub: Optional[bytes] = None
    remote_pub: Optional[bytes] = None
    n_send: int = 0
    n_recv: int = 0
    pn: int = 0    # previous sending chain length

    @classmethod
    def initialise_as_sender(
        cls, shared_secret: bytes, recipient_pub: bytes,
    ) -> "RatchetState":
        sk = x25519.X25519PrivateKey.generate()
        priv = sk.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub = sk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        rk, ck = _kdf_rk(shared_secret, _dh(priv, recipient_pub))
        return cls(
            root_key=rk,
            sending_chain_key=ck,
            sending_keypair_priv=priv,
            sending_keypair_pub=pub,
            remote_pub=recipient_pub,
        )

    @classmethod
    def initialise_as_receiver(
        cls, shared_secret: bytes, my_signed_pre_priv: bytes,
        my_signed_pre_pub: bytes,
    ) -> "RatchetState":
        return cls(
            root_key=shared_secret,
            sending_keypair_priv=my_signed_pre_priv,
            sending_keypair_pub=my_signed_pre_pub,
        )


@dataclass
class EncryptedMessage:
    header: dict      # {ratchet_pub: hex, n: int, pn: int}
    ciphertext: bytes
    nonce: bytes


def encrypt(state: RatchetState, plaintext: bytes,
            associated_data: bytes = b"") -> EncryptedMessage:
    if state.sending_chain_key is None:
        raise RuntimeError("ratchet not ready to send (no chain key)")
    new_ck, mk = _kdf_ck(state.sending_chain_key)
    state.sending_chain_key = new_ck
    state.n_send += 1
    nonce = secrets.token_bytes(12)
    aes = AESGCM(mk)
    ct = aes.encrypt(nonce, plaintext, associated_data)
    return EncryptedMessage(
        header={
            "ratchet_pub": (state.sending_keypair_pub or b"").hex(),
            "n": state.n_send,
            "pn": state.pn,
        },
        ciphertext=ct,
        nonce=nonce,
    )


def decrypt(state: RatchetState, msg: EncryptedMessage,
            associated_data: bytes = b"") -> bytes:
    """Decrypt a single message. If the header carries a new ratchet
    public key, perform a DH-ratchet step before deriving the
    message key."""
    incoming_pub = bytes.fromhex(msg.header["ratchet_pub"])
    if (state.remote_pub != incoming_pub
            and state.sending_keypair_priv is not None):
        # DH ratchet step — derive a new receiving chain
        dh_out = _dh(state.sending_keypair_priv, incoming_pub)
        new_rk, new_recv_ck = _kdf_rk(state.root_key, dh_out)
        state.root_key = new_rk
        state.receiving_chain_key = new_recv_ck
        state.remote_pub = incoming_pub
        state.pn = state.n_send
        state.n_send = 0

        # Then rotate our own sending key
        sk = x25519.X25519PrivateKey.generate()
        new_priv = sk.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        new_pub = sk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        state.sending_keypair_priv = new_priv
        state.sending_keypair_pub = new_pub
        rk2, send_ck = _kdf_rk(state.root_key, _dh(new_priv, incoming_pub))
        state.root_key = rk2
        state.sending_chain_key = send_ck

    if state.receiving_chain_key is None:
        raise RuntimeError("no receiving chain established yet")

    new_ck, mk = _kdf_ck(state.receiving_chain_key)
    state.receiving_chain_key = new_ck
    state.n_recv += 1
    aes = AESGCM(mk)
    return aes.decrypt(msg.nonce, msg.ciphertext, associated_data)


# ── Convenience: full bundle for server upload ─────────────────────


@dataclass
class KeyBundle:
    """Public-only material an offline user uploads to the server."""
    identity_pub: bytes
    signing_pub: bytes               # Ed25519 — verifies signed_pre_sig
    signed_pre_pub: bytes
    signed_pre_id: int
    signed_pre_sig: bytes
    one_time_pre_pubs: list[tuple[int, bytes]] = field(default_factory=list)


def fresh_bundle() -> tuple[IdentityKey, SigningKey, PreKey,
                              list[PreKey], KeyBundle]:
    """Generate a brand-new identity + Ed25519 signing key +
    signed pre-key + 100 one-time pre-keys. Returns private-side
    pieces plus the public ``KeyBundle`` ready for server upload."""
    ident = IdentityKey.generate()
    sign = SigningKey.generate()
    spk = PreKey.generate(pre_key_id=int(time.time()) & 0xFFFFFFFF,
                          signing_key=sign)
    onetime: list[PreKey] = []
    for i in range(100):
        onetime.append(PreKey.generate(pre_key_id=i + 1))
    bundle = KeyBundle(
        identity_pub=ident.public,
        signing_pub=sign.public,
        signed_pre_pub=spk.public,
        signed_pre_id=spk.pre_key_id,
        signed_pre_sig=spk.signature or b"",
        one_time_pre_pubs=[(p.pre_key_id, p.public) for p in onetime],
    )
    return ident, sign, spk, onetime, bundle
