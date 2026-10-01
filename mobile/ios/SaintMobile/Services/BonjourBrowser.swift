import Foundation
import Network
import SaintCore

/// Finds SAINT on your Wi-Fi: the desktop advertises `_saint._tcp` with its device id in the TXT record. Each
/// result is resolved to an address once and reported, so a paired PC is reachable without typing anything.
final class BonjourBrowser {
    struct Found: Equatable {
        var id: String
        var name: String
        var host: String
        var port: Int
    }

    var onFound: ((Found) -> Void)?
    private var browser: NWBrowser?
    private var resolving = Set<String>()
    private let queue = DispatchQueue(label: "app.saint.bonjour")

    func start() {
        if browser != nil { return }
        let params = NWParameters()
        params.includePeerToPeer = true
        let browser = NWBrowser(for: .bonjourWithTXTRecord(type: "_saint._tcp", domain: nil), using: params)
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            for result in results { self?.handle(result) }
        }
        browser.stateUpdateHandler = { state in
            if case .failed = state { browser.cancel() }
        }
        browser.start(queue: queue)
        self.browser = browser
    }

    func stop() {
        browser?.cancel()
        browser = nil
    }

    private func handle(_ result: NWBrowser.Result) {
        guard case .bonjour(let txt) = result.metadata else { return }
        let id = txt["id"] ?? ""
        let name = txt["name"] ?? ""
        if id.isEmpty { return }
        let key = "\(id)|\(result.endpoint)"
        if resolving.contains(key) { return }
        resolving.insert(key)
        resolve(endpoint: result.endpoint) { [weak self] host, port in
            guard let self = self else { return }
            self.queue.async { self.resolving.remove(key) }
            if let host = host, let port = port {
                self.onFound?(Found(id: id, name: name, host: host, port: port))
            }
        }
    }

    /// Connecting to the service endpoint makes the system resolve it; the path then tells us the address.
    private func resolve(endpoint: NWEndpoint, completion: @escaping (String?, Int?) -> Void) {
        let connection = NWConnection(to: endpoint, using: .tcp)
        var finished = false
        let finish: (String?, Int?) -> Void = { host, port in
            if finished { return }
            finished = true
            connection.cancel()
            completion(host, port)
        }
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready:
                if case .hostPort(let host, let port)? = connection.currentPath?.remoteEndpoint {
                    var text = "\(host)"
                    if let percent = text.firstIndex(of: "%") { text = String(text[..<percent]) }   // drop an IPv6 zone
                    finish(text, Int(port.rawValue))
                } else {
                    finish(nil, nil)
                }
            case .failed, .cancelled:
                finish(nil, nil)
            default:
                break
            }
        }
        connection.start(queue: queue)
        queue.asyncAfter(deadline: .now() + 6) { finish(nil, nil) }
    }
}
