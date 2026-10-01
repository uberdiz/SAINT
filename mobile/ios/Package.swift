// swift-tools-version:5.9
import PackageDescription

// SaintCore: everything in SAINT Mobile that isn't user interface — the encrypted link to your PC,
// sync, language understanding, reminders, the on-device intent router. Pure Swift on Foundation and
// CryptoKit (swift-crypto on Linux/Windows), so `swift test` runs it without a phone or simulator.
let package = Package(
    name: "SaintCore",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "SaintCore", targets: ["SaintCore"]),
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-crypto.git", from: "3.0.0"),
    ],
    targets: [
        .target(
            name: "SaintCore",
            dependencies: [
                .product(name: "Crypto", package: "swift-crypto", condition: .when(platforms: [.linux, .windows])),
            ],
            resources: [.copy("Resources/Lexicon")]
        ),
        .testTarget(
            name: "SaintCoreTests",
            dependencies: ["SaintCore"],
            resources: [.copy("Resources")]
        ),
    ],
    swiftLanguageVersions: [.v5]
)
