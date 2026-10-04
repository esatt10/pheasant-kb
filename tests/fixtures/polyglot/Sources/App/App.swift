import Foundation
@testable import AppCore

struct Item { let id: String }

protocol Renderer { func render() -> String }

final class App: Renderer {
    func render() -> String {
        return format(Store.load("x"))
    }
}

extension App { func reset() { Store.clear() } }
