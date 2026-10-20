// Local-development broker: its bytes remain stable while the app is rebuilt.
// The login Keychain remains the only location that stores the Google session.
import Foundation
import Security
import CryptoKit

struct Request: Decodable {
    let operation: String
    let account: String
    let value: String?
    let interactive: Bool?
}
struct Response: Encodable {
    let status: Int32
    let value: String?
}
let service = "com.permafrost.desktop.google"

func authorizedParent() -> Bool {
    let pid = getppid()
    guard pid > 1 else { return false }
    var ownCode: SecCode?
    var ownStatic: SecStaticCode?
    var info: CFDictionary?
    guard SecCodeCopySelf([], &ownCode) == errSecSuccess,
          let ownCode,
          SecCodeCopyStaticCode(ownCode, [], &ownStatic) == errSecSuccess,
          let ownStatic,
          SecCodeCopySigningInformation(ownStatic, SecCSFlags(rawValue: kSecCSSigningInformation), &info) == errSecSuccess,
          let certificates = (info as? [String: Any])?[kSecCodeInfoCertificates as String] as? [SecCertificate],
          let certificate = certificates.first else { return false }
    let fingerprint = Insecure.SHA1.hash(data: SecCertificateCopyData(certificate) as Data)
        .map { String(format: "%02x", $0) }.joined()
    var requirement: SecRequirement?
    let expression = "identifier \"com.permafrost.desktop\" and certificate leaf = H\"\(fingerprint)\""
    guard SecRequirementCreateWithString(expression as CFString, [], &requirement) == errSecSuccess,
          let requirement else { return false }
    var parent: SecCode?
    let attributes = [kSecGuestAttributePid as String: pid] as CFDictionary
    return SecCodeCopyGuestWithAttributes(nil, attributes, [], &parent) == errSecSuccess
        && parent != nil
        && SecCodeCheckValidity(parent!, SecCSFlags(rawValue: kSecCSStrictValidate), requirement) == errSecSuccess
        && getppid() == pid
}

func perform(_ request: Request) -> Response {
    guard authorizedParent(), request.account.count <= 512,
          request.account.hasSuffix(".apps.googleusercontent.com"),
          ["get", "set", "delete"].contains(request.operation) else {
        return Response(status: errSecAuthFailed, value: nil)
    }
    // Unattended regressions fail with an OSStatus instead of waiting on UI.
    if request.interactive == false { SecKeychainSetUserInteractionAllowed(false) }
    var item: SecKeychainItem?
    var count: UInt32 = 0
    var bytes: UnsafeMutableRawPointer?
    let status = service.withCString { serviceBytes in
        request.account.withCString { accountBytes in
            if request.operation == "get" {
                return SecKeychainFindGenericPassword(nil, UInt32(service.utf8.count), serviceBytes,
                UInt32(request.account.utf8.count), accountBytes,
                &count, &bytes, &item)
            }
            return SecKeychainFindGenericPassword(nil, UInt32(service.utf8.count), serviceBytes,
                UInt32(request.account.utf8.count), accountBytes, nil, nil, &item)
        }
    }
    if request.operation == "get" {
        defer { if let bytes { SecKeychainItemFreeContent(nil, bytes) } }
        guard status == errSecSuccess, let bytes, count <= 65536, authorizedParent() else {
            return Response(status: status == errSecSuccess ? errSecAuthFailed : status, value: nil)
        }
        return Response(status: errSecSuccess,
            value: String(data: Data(bytes: bytes, count: Int(count)), encoding: .utf8))
    }
    if request.operation == "delete" {
        return Response(status: status == errSecSuccess ? SecKeychainItemDelete(item!) : status, value: nil)
    }
    guard let value = request.value, !value.isEmpty, value.utf8.count <= 65536 else {
        return Response(status: errSecParam, value: nil)
    }
    let data = Data(value.utf8)
    let result = data.withUnsafeBytes { contents -> OSStatus in
        if status == errSecSuccess {
            return SecKeychainItemModifyAttributesAndData(item!, nil, UInt32(data.count), contents.baseAddress)
        }
        guard status == errSecItemNotFound else { return status }
        return service.withCString { serviceBytes in
            request.account.withCString { accountBytes in
                SecKeychainAddGenericPassword(nil, UInt32(service.utf8.count), serviceBytes,
                    UInt32(request.account.utf8.count), accountBytes,
                    UInt32(data.count), contents.baseAddress!, nil)
            }
        }
    }
    return Response(status: result, value: nil)
}

// The app validates this running process before sending any credential bytes.
FileHandle.standardOutput.write(Data("PF1\n".utf8))
let input = FileHandle.standardInput.readData(ofLength: 65537)
let response: Response
if input.count <= 65536, let request = try? JSONDecoder().decode(Request.self, from: input) {
    response = perform(request)
} else {
    response = Response(status: errSecParam, value: nil)
}
if let output = try? JSONEncoder().encode(response) {
    FileHandle.standardOutput.write(output)
}
