// swift-tools-version: 6.0
import PackageDescription

// Resolve the same vetted Sparkle signing tools used by the app build.
let package = Package(
    name: "BreachReleaseTools",
    platforms: [.macOS("27.0")],
    dependencies: [.package(url: "https://github.com/sparkle-project/Sparkle.git", exact: "2.10.0")],
    targets: [.executableTarget(name: "ReleaseKey", dependencies: [.product(name: "Sparkle", package: "Sparkle")],
                                path: "scripts", sources: ["public-key.swift"])]
)
