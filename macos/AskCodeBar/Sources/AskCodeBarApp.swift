// AskCode 메뉴 막대 앱.
// 화면만 맡고, 동작은 검증된 CLI(`askcode status --json`, `host on|off`, `provider`, `backup`)를 부른다.
// 인증은 .env의 ASKCODE_LLM_PROVIDER가 정한다(API 키 | 로컬 claude CLI 구독). 상태 조회는 Claude를 부르지 않는다(비용 0).

import AppKit
import SwiftUI
import UserNotifications

// MARK: - 상태 모델 (askcode status --json)

struct JobState: Decodable {
    let state: String          // running | waiting | off
    let pid: Int?
    let last_exit: String?
    let last_log: String?
}

struct Money: Decodable {
    let questions: Int
    let cost: Double
}

struct RecentQuestion: Decodable, Identifiable {
    let id: String
    let question: String
    let route: String
    let provider: String?   // api | subscription — 답변 시점의 인증 경로
    let confidence: String?
    let ts: Double
    let cost: Double
    let flags: [String]
}

struct BackupInfo: Decodable {
    let name: String
    let age_hours: Double
    let offsite: Bool
}

struct ClaudeCheck: Decodable {
    let ok: Bool
    let problem: String?
}

struct AppStatus: Decodable {
    let jobs: [String: JobState]
    let installed: Bool
    let console_url: String
    let auth: String
    let provider: String?       // api | subscription
    let claude_cli: Bool?       // subscription 모드일 때 claude 바이너리 존재 여부
    let today: Money
    let week: Money
    let check: Int
    let recent: [RecentQuestion]
    let last_backup: BackupInfo?
    let claude: ClaudeCheck?
}

enum Health {
    case running, restarting, off, unknown

    var label: String {
        switch self {
        case .running: return "실행 중"
        case .restarting: return "재시작 대기"
        case .off: return "꺼짐"
        case .unknown: return "확인 중"
        }
    }

    var symbol: String {
        switch self {
        case .running: return "bubble.left.and.text.bubble.right.fill"
        case .restarting: return "exclamationmark.bubble.fill"
        case .off: return "bubble.left.and.bubble.right"
        case .unknown: return "ellipsis.bubble"
        }
    }

    var color: Color {
        switch self {
        case .running: return .green
        case .restarting: return .orange
        case .off: return .secondary
        case .unknown: return .secondary
        }
    }
}

// MARK: - CLI 실행

enum CLI {
    static var repo: String {
        (Bundle.main.object(forInfoDictionaryKey: "AskCodeRepo") as? String).flatMap { $0.isEmpty ? nil : $0 }
            ?? NSString(string: "~/Documents/projects/askcode").expandingTildeInPath
    }

    static var uv: String {
        if let v = Bundle.main.object(forInfoDictionaryKey: "AskCodeUV") as? String, !v.isEmpty { return v }
        for p in ["/opt/homebrew/bin/uv", "/usr/local/bin/uv"] where FileManager.default.isExecutableFile(atPath: p) {
            return p
        }
        return "/opt/homebrew/bin/uv"
    }

    /// `uv run --project <repo> askcode <args>`. 셸을 거치지 않고, 셸 환경(ANTHROPIC_* 등)을 물려주지 않는다.
    static func run(_ args: [String]) async -> (code: Int32, out: String, err: String) {
        await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: uv)
                p.arguments = ["run", "--project", repo, "askcode"] + args
                p.currentDirectoryURL = URL(fileURLWithPath: repo)
                p.environment = ["PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                                 "HOME": NSHomeDirectory(), "PYTHONUNBUFFERED": "1"]
                let out = Pipe(), err = Pipe()
                p.standardOutput = out
                p.standardError = err
                do {
                    try p.run()
                } catch {
                    cont.resume(returning: (-1, "", "실행 실패: \(error.localizedDescription)"))
                    return
                }
                let o = out.fileHandleForReading.readDataToEndOfFile()
                let e = err.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                cont.resume(returning: (p.terminationStatus, String(decoding: o, as: UTF8.self),
                                        String(decoding: e, as: UTF8.self)))
            }
        }
    }
}

// MARK: - 상태 저장소

@MainActor
final class Store: ObservableObject {
    @Published var status: AppStatus?
    @Published var busy: String?          // 진행 중인 작업 이름
    @Published var message: String?      // 마지막 결과·오류
    @Published var claude: ClaudeCheck?
    @Published var lastUpdated: Date?
    @Published var consoleUp = false     // 127.0.0.1:8787이 실제 응답하는지(launchd 상태와 별개로 수동 실행도 잡는다)

    private var timer: Timer?
    private var userToggled = false

    var health: Health {
        guard let s = status, let bot = s.jobs["bot"] else { return .unknown }
        switch bot.state {
        case "running": return .running
        case "waiting": return .restarting
        default: return .off
        }
    }

    init() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func refresh() {
        Task {
            let r = await CLI.run(["status", "--json"])
            guard r.code == 0, let data = r.out.data(using: .utf8),
                  let s = try? JSONDecoder().decode(AppStatus.self, from: data) else {
                message = "상태를 읽지 못했어요: " + (r.err.split(separator: "\n").last.map(String.init) ?? "알 수 없는 오류")
                return
            }
            let wasRunning = health == .running
            status = s
            lastUpdated = Date()
            probeConsole()
            if wasRunning && health != .running && !userToggled {
                notify("AskCode 봇이 멈췄어요", "launchd가 다시 띄우는 중이에요. 계속되면 로그를 확인하세요.")
            }
            userToggled = false
        }
    }

    func toggle() {
        let turnOn = health == .off || health == .unknown
        userToggled = true
        perform(turnOn ? "켜는 중" : "끄는 중", ["host", turnOn ? "on" : "off"]) { r in
            r.code == 0 ? (turnOn ? "켰어요. 봇이 슬랙에 연결되기까지 10초쯤 걸려요." : "껐어요. 잠자기 방지도 풀렸어요.")
                        : "실패: " + r.err.suffix(200)
        }
    }

    func setProvider(_ mode: String) {
        guard mode != status?.provider else { return }
        perform("인증 경로 바꾸는 중", ["provider", mode]) { r in
            r.code == 0 ? (r.out.split(separator: "\n").first.map(String.init) ?? "바꿨어요.")
                        : "전환 실패: " + r.err.suffix(200)
        }
    }

    func backupNow() {
        perform("백업 중", ["backup"]) { r in
            r.code == 0 ? (r.out.split(separator: "\n").last.map(String.init) ?? "백업 완료") : "백업 실패: " + r.err.suffix(200)
        }
    }

    func checkClaude() {
        busy = "크레딧 확인 중"
        Task {
            let r = await CLI.run(["status", "--json", "--check-claude"])
            busy = nil
            if let data = r.out.data(using: .utf8), let s = try? JSONDecoder().decode(AppStatus.self, from: data) {
                status = s
                claude = s.claude
                message = s.claude?.ok == true ? "Claude API 키·크레딧 정상" : "Claude 호출 불가: \(s.claude?.problem ?? "-")"
            } else {
                message = "확인 실패: " + r.err.suffix(200)
            }
        }
    }

    private func perform(_ label: String, _ args: [String], done: @escaping ((code: Int32, out: String, err: String)) -> String) {
        busy = label
        Task {
            let r = await CLI.run(args)
            busy = nil
            message = done(r)
            refresh()
        }
    }

    /// 콘솔이 실제로 응답하는지 본다. HTTP 응답(상태 코드 무관)이 오면 켜져 있는 것.
    private func probeConsole() {
        Task {
            consoleUp = await consoleReachable()
        }
    }

    private func consoleReachable() async -> Bool {
        guard let base = status?.console_url, let url = URL(string: base) else { return false }
        var req = URLRequest(url: url)
        req.timeoutInterval = 1.5
        return (try? await URLSession.shared.data(for: req)) != nil
    }

    func open(_ path: String) {
        guard let base = status?.console_url, let url = URL(string: base + path) else { return }
        if !consoleUp {
            message = "웹 콘솔이 꺼져 있어요. '켜기'로 켜거나 콘솔 작업 상태를 확인하세요."
            return
        }
        NSWorkspace.shared.open(url)
    }

    func openConsole() {
        Task {
            if await consoleReachable(), let url = URL(string: status?.console_url ?? "") {
                NSWorkspace.shared.open(url)
            } else {
                consoleUp = false
                message = "웹 콘솔이 꺼져 있어요. '켜기'로 켜거나 콘솔 작업 상태를 확인하세요."
            }
        }
    }

    func openLogs() {
        NSWorkspace.shared.open(URL(fileURLWithPath: CLI.repo + "/data/logs"))
    }

    private func notify(_ title: String, _ body: String) {
        let c = UNMutableNotificationContent()
        c.title = title
        c.body = body
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }
}

// MARK: - 화면

struct StatCard: View {
    let title: String
    let value: String
    var accent: Color = .primary
    var action: (() -> Void)?

    var body: some View {
        let content = VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(.title3, design: .rounded).weight(.semibold)).foregroundStyle(accent)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.05)))
        if let action {
            Button(action: action) { content }.buttonStyle(.plain)
        } else {
            content
        }
    }
}

struct JobRow: View {
    let name: String
    let job: JobState?
    var up: Bool? = nil  // 웹 콘솔만: 실제 HTTP 응답 여부(수동 실행·기동 지연을 구분하기 위함)

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(name).font(.callout)
            Spacer()
            Text(text).font(.caption).foregroundStyle(.secondary)
        }
    }

    private var color: Color {
        if let up { return up ? .green : (job?.state == "running" ? .orange : .gray) }
        switch job?.state {
        case "running": return .green
        case "waiting": return name == "백업" ? .blue : .orange
        default: return .gray
        }
    }

    private var text: String {
        if let up {
            if up { return "실행 중" }
            return job?.state == "running" ? "응답 없음" : "꺼짐"
        }
        switch job?.state {
        case "running": return "실행 중"
        case "waiting": return name == "백업" ? "매일 03:30" : "재시작 대기"
        default: return "꺼짐"
        }
    }
}

struct PanelView: View {
    @ObservedObject var store: Store

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Divider()
            if let s = store.status {
                VStack(spacing: 4) {
                    JobRow(name: "봇", job: s.jobs["bot"])
                    JobRow(name: "웹 콘솔", job: s.jobs["console"], up: store.consoleUp)
                    JobRow(name: "백업", job: s.jobs["backup"])
                }
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
                    StatCard(title: "오늘 질문", value: "\(s.today.questions)건")
                    StatCard(title: "오늘 비용", value: money(s.today.cost))
                    StatCard(title: "이번 주 비용", value: money(s.week.cost))
                    StatCard(title: "확인 필요", value: "\(s.check)건", accent: s.check > 0 ? .orange : .primary) {
                        store.open("/check")
                    }
                }
                recent(s)
                footer(s)
            } else {
                Text("상태를 불러오는 중…").foregroundStyle(.secondary)
            }
            if let m = store.message {
                Text(m).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            HStack {
                Button("웹 콘솔") { store.openConsole() }
                Button("로그") { store.openLogs() }
                Spacer()
                Button("종료") { NSApplication.shared.terminate(nil) }
            }
            .controlSize(.small)
        }
        .padding(14)
        .frame(width: 360)
        .onAppear { store.refresh() }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 8) {
            Image(systemName: store.health.symbol).foregroundStyle(store.health.color).font(.title2)
            VStack(alignment: .leading, spacing: 0) {
                Text("AskCode").font(.headline)
                Text(store.busy ?? "봇 \(store.health.label)" + ((store.status?.provider).map { " · \(providerLabel($0))" } ?? ""))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if store.busy != nil {
                ProgressView().controlSize(.small)
            }
            Button(store.health == .off || store.health == .unknown ? "켜기" : "끄기") { store.toggle() }
                .buttonStyle(.borderedProminent)
                .tint(store.health == .off ? .accentColor : .gray)
                .disabled(store.busy != nil)
        }
    }

    private func recent(_ s: AppStatus) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("최근 질문").font(.caption).foregroundStyle(.secondary)
            if s.recent.isEmpty {
                Text("아직 슬랙 질문이 없어요").font(.callout).foregroundStyle(.secondary)
            }
            ForEach(s.recent.prefix(5)) { q in
                Button {
                    store.open("/q/\(q.id)")
                } label: {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(q.question).font(.callout).lineLimit(1).truncationMode(.tail)
                        Text("\(timeAgo(q.ts)) · \(q.route)\(q.provider.map { " · " + providerLabel($0) } ?? "")"
                             + " · \(confidence(q.confidence)) · \(money(q.cost))"
                             + (q.flags.isEmpty ? "" : " · ⚠︎"))
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func footer(_ s: AppStatus) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: "externaldrive").foregroundStyle(.secondary)
                Text(s.last_backup.map { "백업 \(Int($0.age_hours.rounded()))시간 전" + ($0.offsite ? "" : " · 서버 밖 복사 없음") }
                     ?? "백업 없음")
                    .font(.caption)
                Spacer()
                Button("지금 백업") { store.backupNow() }.controlSize(.small).disabled(store.busy != nil)
            }
            HStack(spacing: 6) {
                Image(systemName: s.provider == "subscription" ? "terminal" : "key")
                    .foregroundStyle(.secondary)
                Picker("", selection: Binding(
                    get: { s.provider ?? "api" },
                    set: { store.setProvider($0) })) {
                    Text("API").tag("api")
                    Text("구독").tag("subscription")
                }
                .pickerStyle(.segmented)
                .frame(width: 104)
                .disabled(store.busy != nil)
                Text(authText(s)).font(.caption).lineLimit(1).truncationMode(.tail)
                Spacer()
                Button(s.provider == "subscription" ? "로그인 확인" : "크레딧 확인") { store.checkClaude() }
                    .controlSize(.small).disabled(store.busy != nil)
            }
        }
    }

    private func providerLabel(_ p: String) -> String { p == "subscription" ? "구독" : "API" }

    private func authText(_ s: AppStatus) -> String {
        if s.provider == "subscription" {
            if s.claude_cli == false { return "claude CLI 없음" }
            guard let c = store.claude else { return "로컬 구독 로그인" }
            return c.ok ? "구독 로그인 정상" : "로그인 필요 (claude auth login)"
        }
        if s.auth != "api_key" { return "API 키 없음 (.env)" }
        guard let c = store.claude else { return "Claude API 키" }
        return c.ok ? "API 키 · 크레딧 정상" : "Claude 호출 불가"
    }

    private func money(_ v: Double) -> String { v < 1 ? String(format: "$%.4f", v) : String(format: "$%.2f", v) }

    private func confidence(_ c: String?) -> String {
        switch c { case "high": return "🟢"; case "medium": return "🟡"; case "low": return "🔴"; default: return "–" }
    }

    private func timeAgo(_ ts: Double) -> String {
        let m = Int((Date().timeIntervalSince1970 - ts) / 60)
        if m < 1 { return "방금" }
        if m < 60 { return "\(m)분 전" }
        if m < 1440 { return "\(m / 60)시간 전" }
        return "\(m / 1440)일 전"
    }
}

@main
struct AskCodeBarApp: App {
    @StateObject private var store = Store()

    var body: some Scene {
        MenuBarExtra {
            PanelView(store: store)
        } label: {
            Image(systemName: store.health.symbol)
            if let s = store.status, s.check > 0 {
                Text("\(s.check)")
            }
        }
        .menuBarExtraStyle(.window)
    }
}
