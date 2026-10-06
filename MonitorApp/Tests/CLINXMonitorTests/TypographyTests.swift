import AppKit
import SwiftUI
import XCTest
@testable import CLINXMonitor

@MainActor
final class TypographyTests: XCTestCase {
    func testPreferencesPersistSeparatelyAndResetWithoutTouchingOtherSettings() throws {
        let suite = "CLINXMonitorTests.typography.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set("https://unchanged.example", forKey: "monitor.observerEndpoint")
        let preferences = TypographyPreferences(store: defaults)
        XCTAssertEqual(preferences.style, TypographyStyle())
        preferences.family = .serif
        preferences.weight = .medium
        preferences.interfaceSize = 14
        preferences.contentSize = 18
        preferences.codeSize = 12
        preferences.lineSpacing = 6
        let reloaded = TypographyPreferences(store: try XCTUnwrap(UserDefaults(suiteName: suite)))
        XCTAssertEqual(reloaded.style, preferences.style)
        XCTAssertEqual(reloaded.style.interfaceScale, 14 / 12.0, accuracy: 0.001)
        XCTAssertEqual(reloaded.style.bodySize, 18)
        XCTAssertEqual(reloaded.style.monoSize, 12)
        XCTAssertEqual(reloaded.style.paragraphSpacing, 6)
        reloaded.reset()
        XCTAssertEqual(TypographyPreferences(store: defaults).style, TypographyStyle())
        XCTAssertEqual(defaults.string(forKey: "monitor.observerEndpoint"), "https://unchanged.example")
    }

    func testInterfaceAndContentFontsRemainIndependentAndPreserveMonospacedIDs() {
        let original = TypographyStyle()
        var bodyOnly = original
        bodyOnly.contentSize = 22
        bodyOnly.codeSize = 18
        XCTAssertEqual(DS.Font.rowTitle.resolved(in: original), DS.Font.rowTitle.resolved(in: bodyOnly))
        var interfaceOnly = original
        interfaceOnly.interfaceSize = 16
        XCTAssertNotEqual(DS.Font.rowTitle.resolved(in: original), DS.Font.rowTitle.resolved(in: interfaceOnly))
        XCTAssertEqual(interfaceOnly.bodySize, original.bodySize)
        var serif = original
        serif.family = .serif
        XCTAssertEqual(DS.Font.monoID.resolved(in: serif), DS.Font.monoID.resolved(in: original))
        XCTAssertEqual(TextFontWeight.regular.resolve(.semibold), .semibold)
        XCTAssertEqual(TextFontWeight.medium.resolve(.regular), .medium)
        XCTAssertEqual(TextFontWeight.medium.resolve(.semibold), .semibold)
    }

    func testInvalidStoredSizesStayWithinReadableLimits() {
        let style = TypographyStyle(interfaceSize: 99, contentSize: .nan, codeSize: -10, lineSpacing: .infinity)
        XCTAssertEqual(style.interfaceScale, 1.5)
        XCTAssertEqual(style.bodySize, 15)
        XCTAssertEqual(style.monoSize, 10)
        XCTAssertEqual(style.paragraphSpacing, 4)
    }

    func testPersistedInterfaceSizeAffectsFontTokensAtRenderTime() throws {
        let suite = "CLINXMonitorTests.typography-render.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let preferences = TypographyPreferences(store: defaults)
        func height() -> CGFloat {
            NSHostingView(rootView: Text("Interface text").font(DS.Font.rowTitle)
                .defaultAppStorage(defaults)).fittingSize.height
        }
        let normal = height()
        preferences.interfaceSize = 18
        XCTAssertGreaterThan(height(), normal, "The font token must read the selected local size")
    }
}
