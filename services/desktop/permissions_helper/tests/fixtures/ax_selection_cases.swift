// Append to an extracted actual axSelectionRange body in a Foundation test executable.
func selected(_ value: String, _ text: String, prefix: String? = nil, suffix: String? = nil,
              selection: String = "text", location: Int, length: Int) throws {
    var req: [String: Any] = ["text": text, "selection_type": selection]
    if let prefix = prefix { req["prefix"] = prefix }
    if let suffix = suffix { req["suffix"] = suffix }
    let result = try axSelectionRange(req, value: value)
    precondition(result.location == location && result.length == length,
                 "unexpected range \(result.location),\(result.length)")
}
func rejected(_ value: String, _ req: [String: Any]) {
    do { _ = try axSelectionRange(req, value: value); preconditionFailure("expected rejection") }
    catch { precondition(!String(describing: error).contains("secret-context")) }
}
try selected("before needle after", "needle", location: 7, length: 6)
try selected("😀a🦄end", "🦄", location: 3, length: 2)
try selected("😀a🦄end", "🦄", selection: "cursor_before", location: 3, length: 0)
try selected("😀a🦄end", "🦄", selection: "cursor_after", location: 5, length: 0)
try selected("a\u{301}b", "a\u{301}", location: 0, length: 2)
try selected("first cat; second cat!", "cat", prefix: "second ", suffix: "!", location: 18, length: 3)
try selected("  ", " ", prefix: " ", location: 1, length: 1)
try selected("aaaa", "aa", prefix: "aa", location: 2, length: 2)
try selected(String(repeating: "x", count: 500) + "needle", "needle", location: 500, length: 6)
rejected("cat cat", ["text": "cat"])
rejected("aaaa", ["text": "aa"])
rejected("😀😀😀", ["text": "😀😀"])
rejected("needle", ["text": "Needle"])
rejected("á", ["text": "a\u{301}"])
rejected("a\u{301}needle", ["text": "needle", "prefix": "á"])
rejected("needlea\u{301}", ["text": "needle", "suffix": "á"])
rejected("cat!", ["text": "cat", "prefix": "secret-context"])
rejected("cat", ["text": ""])
rejected("cat", ["text": "cat", "prefix": 1])
rejected("cat", ["text": "cat", "suffix": NSNull()])
rejected("cat", ["text": "cat", "selection_type": "unknown"])
rejected("cat", ["text": "cat", "selection_type": 1])
rejected("cat", ["text": String(repeating: "x", count: 10_001)])
print("AX selection matching: 23 assertions passed")
