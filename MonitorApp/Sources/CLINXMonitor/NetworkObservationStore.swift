import Foundation
import Combine

@MainActor
final class NetworkObservationStore: ObservableObject {
    @Published private(set) var items: [NetworkObservation] = []
    @Published private(set) var detail: ObservationDetail?
    @Published private(set) var turns: [ObservationTurn] = []
    @Published private(set) var context: ObservationContext?
    @Published private(set) var nextCursor: String?
    @Published private(set) var historyCursor: String?
    @Published private(set) var error: String?
    @Published private(set) var coverage: [String: String] = [:]
    @Published private(set) var retentionEvicted = 0
    @Published private(set) var sources: [ObservationSource] = []
    @Published private(set) var selectedID: String?
    @Published var filters = ObservationFilters()
    private var client: (any NetworkObservationServing)?
    private var listGeneration = 0
    private var detailGeneration = 0
    private var refreshing = false
    private var loadingHistory = false
    private var loadingMore = false
    private var loadingContext = false
    private var lastFilters = ObservationFilters()
    private(set) var pausedHistoryRefresh = false

    init(client: (any NetworkObservationServing)? = nil) { self.client = client }

    func configure(endpoint: String, account: String) {
        listGeneration += 1
        detailGeneration += 1
        items = []; sources = []; detail = nil; turns = []; context = nil
        selectedID = nil; nextCursor = nil; historyCursor = nil
        client = nil
        guard let url = URL(string: endpoint) else { error = "请配置中心 Observer"; return }
        do { client = try ObserverClient(baseURL: url, credentialAccount: account); error = nil }
        catch { self.error = "中心 Observer 配置不可用" }
    }

    func refresh() async {
        guard let client, !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        listGeneration += 1
        let generation = listGeneration
        let requestedFilters = filters
        if lastFilters != requestedFilters {
            items = []; nextCursor = nil
            lastFilters = requestedFilters
        }
        do {
            let desiredCount = max(50, min(500, items.count))
            let page = try await client.observations(filters: requestedFilters, cursor: nil)
            var refreshed = page.items
            var cursor = page.nextCursor
            var pages = 1
            // Revalidate every displayed page after grants change, while keeping
            // the visible list stable for users who loaded more than one page.
            while refreshed.count < desiredCount, let next = cursor, pages < 10 {
                let more = try await client.observations(filters: requestedFilters, cursor: next)
                let seen = Set(refreshed.map(\.id))
                refreshed.append(contentsOf: more.items.filter { !seen.contains($0.id) })
                cursor = more.nextCursor
                pages += 1
            }
            guard generation == listGeneration, requestedFilters == filters else { return }
            items = Array(refreshed.prefix(500))
            nextCursor = cursor
            coverage = page.coverage
            retentionEvicted = page.retentionEvicted
            sources = page.sources ?? []
            error = nil
            if let id = selectedID { await refreshDetail(id, client: client) }
        } catch {
            guard generation == listGeneration else { return }
            self.error = "全网目录暂不可用：\(error)"
            // Fail closed on auth errors; an offline server is shown as disconnected.
            items = []; sources = []; detail = nil; turns = []; context = nil
        }
    }

    func loadMore() async {
        guard let client, let cursor = nextCursor, items.count < 500, !loadingMore else { return }
        loadingMore = true
        defer { loadingMore = false }
        let generation = listGeneration, requested = filters
        do {
            let page = try await client.observations(filters: requested, cursor: cursor)
            guard generation == listGeneration, requested == filters else { return }
            let existing = Set(items.map(\.id))
            items.append(contentsOf: page.items.filter { !existing.contains($0.id) }.prefix(500 - items.count))
            nextCursor = page.nextCursor
        } catch { self.error = "分页已失效，请刷新目录" }
    }

    func select(_ id: String) async {
        guard let client else { return }
        detailGeneration += 1
        selectedID = id; detail = nil; turns = []; context = nil; historyCursor = nil
        pausedHistoryRefresh = false
        await refreshDetail(id, client: client)
    }

    private func refreshDetail(_ id: String, client: any NetworkObservationServing) async {
        let generation = detailGeneration
        do {
            let page = try await client.observation(id, cursor: nil)
            guard generation == detailGeneration, selectedID == id else { return }
            detail = page
            if !pausedHistoryRefresh {
                if turns != page.turns { turns = page.turns }
                historyCursor = page.nextCursor
            }
        } catch {
            guard generation == detailGeneration, selectedID == id else { return }
            detail = nil; turns = []; context = nil; historyCursor = nil
            self.error = "详情不可用或授权已撤销"
        }
    }

    func loadHistory() async {
        guard let client, let id = selectedID, let cursor = historyCursor,
              !loadingHistory, turns.count < 512 else { return }
        loadingHistory = true; pausedHistoryRefresh = true
        defer { loadingHistory = false }
        let generation = detailGeneration
        do {
            let page = try await client.observation(id, cursor: cursor)
            guard generation == detailGeneration, selectedID == id else { return }
            let existing = Set(turns.map(\.id))
            turns.append(contentsOf: page.turns.filter { !existing.contains($0.id) }.prefix(512 - turns.count))
            historyCursor = page.nextCursor
        } catch { self.error = "历史分页不可用，请重新选择任务" }
    }

    func loadContext(older: Bool = false) async {
        guard let client, let id = selectedID, !loadingContext else { return }
        loadingContext = true
        defer { loadingContext = false }
        let generation = detailGeneration
        do {
            let page = try await client.observationContext(id, cursor: older ? context?.nextCursor : nil)
            guard generation == detailGeneration, selectedID == id else { return }
            context = page
        } catch { self.error = "源端内容当前不可用；已缓存历史仍可查看" }
    }

    var pollNanoseconds: UInt64 { items.contains(where: \.isRunning) ? 2_000_000_000 : 12_000_000_000 }
}
