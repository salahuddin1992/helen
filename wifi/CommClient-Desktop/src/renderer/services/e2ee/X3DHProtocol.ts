/**
 * X3DH Key Agreement Protocol
 * Initiator → Receiver: shared secret from (IK_A, EK_A, IK_B, SPK_B, OPK_B)
 */
import { CryptoUtils } from './CryptoUtils';

export interface X3DHResult {
  sharedSecret: ArrayBuffer;
  associatedData: ArrayBuffer;
  sentEphemeralKey: JsonWebKey;
}

export class X3DHProtocol {
  /**
   * X3DH key agreement
   * Returns shared secret for Double Ratchet initialization
   */
  static async performKeyAgreement(
    ikA: CryptoKeyPair, // Alice's identity key
    ekA: CryptoKeyPair, // Alice's ephemeral key
    ikBPublic: CryptoKey, // Bob's identity public key
    spkBPublic: CryptoKey, // Bob's signed pre-key
    opkBPublic: CryptoKey | null // Bob's one-time pre-key (optional)
  ): Promise<X3DHResult> {
    // Derive shared secrets from each DH exchange
    const dh1 = await CryptoUtils.deriveSharedSecret(ikA.privateKey, spkBPublic);
    const dh2 = await CryptoUtils.deriveSharedSecret(ekA.privateKey, ikBPublic);
    const dh3 = await CryptoUtils.deriveSharedSecret(ekA.privateKey, spkBPublic);

    let dh4: ArrayBuffer | null = null;
    if (opkBPublic) {
      dh4 = await CryptoUtils.deriveSharedSecret(ekA.privateKey, opkBPublic);
    }

    // KDF to derive root key
    const kdfInput = CryptoUtils.concatBuffers(dh1, dh2, dh3, dh4 || new Uint8Array(32));

    const rootKey = await CryptoUtils.hkdf(
      kdfInput,
      new ArrayBuffer(32), // salt
      CryptoUtils.concatBuffers(
        new TextEncoder().encode('X3DH'),
        new Uint8Array(32)
      ),
      32
    );

    // Associated data for AEAD (includes identity keys for binding)
    const associatedData = CryptoUtils.concatBuffers(
      await CryptoUtils.exportPublicKeyJwk(ikA.publicKey).then((jwk) =>
        new TextEncoder().encode(JSON.stringify(jwk))
      ),
      await CryptoUtils.exportPublicKeyJwk(ikBPublic).then((jwk) =>
        new TextEncoder().encode(JSON.stringify(jwk))
      )
    );

    const sentEphemeralKey = await CryptoUtils.exportPublicKeyJwk(ekA.publicKey);

    return {
      sharedSecret: rootKey,
      associatedData,
      sentEphemeralKey,
    };
  }

  /**
   * Receiver side: process X3DH bundle
   */
  static async processKeyAgreement(
    ikB: CryptoKeyPair, // Bob's identity key
    spkB: CryptoKeyPair, // Bob's signed pre-key
    opkB: CryptoKeyPair | null, // Bob's one-time pre-key (optional)
    ikAPublic: CryptoKey, // Alice's identity public key
    ekAPublic: CryptoKey, // Alice's sent ephemeral key
  ): Promise<X3DHResult> {
    const dh1 = await CryptoUtils.deriveSharedSecret(spkB.privateKey, ikAPublic);
    const dh2 = await CryptoUtils.deriveSharedSecret(ikB.privateKey, ekAPublic);
    const dh3 = await CryptoUtils.deriveSharedSecret(spkB.privateKey, ekAPublic);

    let dh4: ArrayBuffer | null = null;
    if (opkB) {
      dh4 = await CryptoUtils.deriveSharedSecret(opkB.privateKey, ekAPublic);
    }

    const kdfInput = CryptoUtils.concatBuffers(dh1, dh2, dh3, dh4 || new Uint8Array(32));

    const rootKey = await CryptoUtils.hkdf(
      kdfInput,
      new ArrayBuffer(32),
      CryptoUtils.concatBuffers(
        new TextEncoder().encode('X3DH'),
        new Uint8Array(32)
      ),
      32
    );

    const associatedData = CryptoUtils.concatBuffers(
      await CryptoUtils.exportPublicKeyJwk(ikAPublic).then((jwk) =>
        new TextEncoder().encode(JSON.stringify(jwk))
      ),
      await CryptoUtils.exportPublicKeyJwk(ikB.publicKey).then((jwk) =>
        new TextEncoder().encode(JSON.stringify(jwk))
      )
    );

    const sentEphemeralKey = await CryptoUtils.exportPublicKeyJwk(ekAPublic);

    return {
      sharedSecret: rootKey,
      associatedData,
      sentEphemeralKey,
    };
  }
}
