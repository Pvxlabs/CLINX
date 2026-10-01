// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CLINXMonitor",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "CLINXMonitor", targets: ["CLINXMonitor"])],
    targets: [
        .executableTarget(name: "CLINXMonitor", path: "Sources/CLINXMonitor"),
        .testTarget(
            name: "CLINXMonitorTests",
            dependencies: ["CLINXMonitor"],
            path: "Tests/CLINXMonitorTests",
            resources: [.process("Fixtures")]
        )
    ]
)
