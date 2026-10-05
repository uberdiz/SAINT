import Foundation
import Network
import SaintCore

/// A TCP connection for SAINT Link on Network.framework.
final class NWTransport: ByteTransport {
    private let connection: NWConnection
    private let queue = DispatchQueue(label: "app.saint.link.transport")
    private var finished = false

    init(host: String, port: Int) {
        let params = NWParameters.tcp
        if let tcp = params.defaultProtocolStack.transportProtocol as? NWProtocolTCP.Options {
            tcp.noDelay = true
            tcp.enableKeepalive = true
            tcp.keepaliveIdle = 20
            tcp.connectionTimeout = 8
        }
        let nwPort = NWEndpoint.Port(rawValue: UInt16(clamping: port)) ?? 8765
        connection = NWConnection(host: NWEndpoint.Host(host), port: nwPort, using: params)
    }

    func connect() async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            var resumed = false
            connection.stateUpdateHandler = { [weak self] state in
                switch state {
                case .ready:
                    if !resumed {
                        resumed = true
                        continuation.resume()
                    }
                case .failed(let error):
                    if !resumed {
                        resumed = true
                        continuation.resume(throwing: LinkError.unreachable("Couldn't connect (\(error.localizedDescription)). "
                            + "Is Local Network allowed for SAINT in Settings, and are you on the same Wi-Fi as your PC?"))
                    }
                    self?.finished = true
                case .waiting:
                    // No route yet. This is also what you see while the "allow Local Network" prompt is on screen,
                    // so keep waiting: the connection timeout (8 s) turns a real dead end into .failed below.
                    break
                case .cancelled:
                    if !resumed {
                        resumed = true
                        continuation.resume(throwing: LinkError.closed)
                    }
                    self?.finished = true
                default:
                    break
                }
            }
            connection.start(queue: queue)
        }
    }

    func send(_ data: Data) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            connection.send(content: data, completion: .contentProcessed { error in
                if let error = error {
                    continuation.resume(throwing: LinkError.unreachable(error.localizedDescription))
                } else {
                    continuation.resume()
                }
            })
        }
    }

    func receive() async throws -> Data {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Data, Error>) in
            connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { data, _, isComplete, error in
                if let error = error {
                    continuation.resume(throwing: LinkError.unreachable(error.localizedDescription))
                } else if let data = data, !data.isEmpty {
                    continuation.resume(returning: data)
                } else if isComplete {
                    continuation.resume(throwing: LinkError.closed)
                } else {
                    continuation.resume(returning: Data())
                }
            }
        }
    }

    func close() {
        connection.cancel()
    }
}
