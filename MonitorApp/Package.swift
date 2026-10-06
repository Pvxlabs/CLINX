// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CLINXMonitor",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "CLINXMonitor", targets: ["CLINXMonitor"])],
    dependencies: [
        .package(url: "https://github.com/gonzalezreal/swift-markdown-ui", exact: "2.4.1")
    ],
    targets: [
        .executableTarget(name: "CLINXMonitor", dependencies: [
            .product(name: "MarkdownUI", package: "swift-markdown-ui")
        ], path: "Sources/CLINXMonitor"),
        .testTarget(
            name: "CLINXMonitorTests",
            dependencies: ["CLINXMonitor"],
            path: "Tests/CLINXMonitorTests",
            resources: [.process("Fixtures")]
        )
    ]
)
