import XCTest
@testable import CLINXMonitor

final class NativeURITests: XCTestCase {
    func testNativeURISurvivesWireDecodingAndEventRefresh() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "api-examples", withExtension: "json"))
        let examples = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let entries = try XCTUnwrap(examples["examples"] as? [[String: Any]])
        let entry = try XCTUnwrap(entries.first { $0["name"] as? String == "pass_detail" })
        var raw = try XCTUnwrap(entry["response"] as? [String: Any])
        let uri = "codex://threads/01a12bb1-f6b4-7d01-9f9b-2b0e42f9308b?hostId=remote-ssh-discovered%3Ap620"
        raw["codex_uri"] = uri
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let task = try decoder.decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: raw))
        XCTAssertEqual(task.codexUri, uri)
        XCTAssertEqual(task.withEvents([], coverage: "PARTIAL", hasMore: false).codexUri, uri)
        raw.removeValue(forKey: "codex_uri")
        let legacy = try decoder.decode(ObservedTask.self, from: JSONSerialization.data(withJSONObject: raw))
        XCTAssertNil(legacy.codexUri)
    }
}
