import CryptoKit
import Foundation

// ASVS 11.2.1: derive the public identity with Apple's vetted Ed25519 implementation.
let text = try String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8)
guard let seed = Data(base64Encoded: text.trimmingCharacters(in: .whitespacesAndNewlines)), seed.count == 32 else {
    throw NSError(domain: "ReleaseSigning", code: 1, userInfo: [NSLocalizedDescriptionKey: "Expected a 32-byte Sparkle signing seed"])
}
let key = try Curve25519.Signing.PrivateKey(rawRepresentation: seed)
print(key.publicKey.rawRepresentation.base64EncodedString())
