import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

struct BridgeError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

func jitter(_ interval: TimeInterval) -> TimeInterval {
    interval * Double.random(in: 0.75...1.25)
}

func attribute(_ element: AXUIElement, _ key: String) throws -> CFTypeRef? {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, key as CFString, &value)
    switch error {
    case .success: return value
    case .noValue, .attributeUnsupported: return nil
    default: throw BridgeError("Cannot read \(key): AX error \(error.rawValue)")
    }
}

func text(_ element: AXUIElement, _ key: String) throws -> String {
    let value = try attribute(element, key)
    if let string = value as? String { return string }
    if let number = value as? NSNumber { return number.stringValue }
    return ""
}

func children(_ element: AXUIElement) throws -> [AXUIElement] {
    return try attribute(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
}

func actions(_ element: AXUIElement) throws -> [String] {
    var values: CFArray?
    let error = AXUIElementCopyActionNames(element, &values)
    guard error == .success || error == .actionUnsupported else {
        throw BridgeError("Cannot read actions: AX error \(error.rawValue)")
    }
    return values as? [String] ?? []
}

func rect(_ element: AXUIElement) throws -> [String: Double] {
    var point = CGPoint.zero
    var size = CGSize.zero
    if let value = try attribute(element, kAXPositionAttribute), CFGetTypeID(value) == AXValueGetTypeID() {
        AXValueGetValue(value as! AXValue, .cgPoint, &point)
    }
    if let value = try attribute(element, kAXSizeAttribute), CFGetTypeID(value) == AXValueGetTypeID() {
        AXValueGetValue(value as! AXValue, .cgSize, &size)
    }
    // UIKit briefly reports infinite geometry while virtualized cells expand.
    // Preserve their text, using the same unavailable-geometry value as missing
    // position/size attributes, so JSON serialization can finish safely.
    guard point.x.isFinite, point.y.isFinite, size.width.isFinite, size.height.isFinite else {
        return ["x": 0, "y": 0, "width": 0, "height": 0]
    }
    return ["x": point.x, "y": point.y, "width": size.width, "height": size.height]
}

func run() throws -> Any {
    let args = CommandLine.arguments
    guard args.count >= 3 else { throw BridgeError("Usage: AccessibilityBridge snapshot|window-info|action bundle-id [JSON]") }
    guard AXIsProcessTrusted() else {
        throw BridgeError("Accessibility permission is required for the executing application in System Settings > Privacy & Security > Accessibility")
    }
    let apps = NSRunningApplication.runningApplications(withBundleIdentifier: args[2])
    guard apps.count == 1, let app = apps.first else {
        throw BridgeError("Expected one running application for \(args[2]), found \(apps.count). Open the iOS BOSS app first")
    }
    let application = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(application, 5)
    guard let windows = try attribute(application, kAXWindowsAttribute) as? [AXUIElement], let window = windows.first else {
        throw BridgeError("Application has no accessible window")
    }
    guard try text(window, kAXRoleAttribute) == kAXWindowRole else {
        throw BridgeError("BOSS is not exposing an accessible window. Unlock the Mac and restore the app, then retry")
    }
    if args[1] == "window-info" {
        let accessibleBounds = try rect(window)
        guard accessibleBounds["width"]! > 0, accessibleBounds["height"]! > 0 else {
            throw BridgeError("Accessible window geometry is not available")
        }
        let windows = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
        var matches = [[String: Any]]()
        for candidate in windows {
            guard let pid = candidate[kCGWindowOwnerPID as String] as? Int32, pid == app.processIdentifier,
                  let number = candidate[kCGWindowNumber as String] as? Int,
                  let bounds = candidate[kCGWindowBounds as String] as? [String: Any],
                  let x = bounds["X"] as? Double, let y = bounds["Y"] as? Double,
                  let width = bounds["Width"] as? Double, let height = bounds["Height"] as? Double else { continue }
            let geometry = ["x": x, "y": y, "width": width, "height": height]
            if geometry.allSatisfy({ key, value in value.isFinite && abs(value - accessibleBounds[key]!) <= 1 }) {
                matches.append(["window_id": number, "bounds": geometry])
            }
        }
        guard matches.count == 1 else {
            throw BridgeError("Expected one Core Graphics window matching the accessibility window, found \(matches.count)")
        }
        return matches[0]
    }
    if args[1] == "snapshot" {
        var nodes = [[String: Any]]()
        func walk(_ element: AXUIElement, _ path: [Int]) throws {
            guard path.count <= 40, nodes.count < 10000 else {
                throw BridgeError("Accessibility tree exceeds supported size; refusing a partial snapshot")
            }
            nodes.append([
                "path": path,
                "role": try text(element, kAXRoleAttribute),
                "title": try text(element, kAXTitleAttribute),
                "description": try text(element, kAXDescriptionAttribute),
                "value": try text(element, kAXValueAttribute),
                "rect": try rect(element),
                "actions": try actions(element),
            ])
            for (index, child) in try children(element).enumerated() {
                try walk(child, path + [index])
            }
        }
        try walk(window, [])
        return nodes
    }
    guard args[1] == "action", args.count == 4,
          let data = args[3].data(using: .utf8),
          let request = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          let path = request["path"] as? [Int],
          let role = request["role"] as? String,
          let description = request["description"] as? String,
          let action = request["action"] as? String else {
        throw BridgeError("Invalid action request")
    }
    let allowedActions = ["AXPress", "AXScrollDownByPage", "AXScrollUpByPage", "AXScrollToVisible", "setValue", "typeText"]
    guard allowedActions.contains(action) else { throw BridgeError("Unsupported action: \(action)") }
    var element = window
    for index in path {
        let list = try children(element)
        guard list.indices.contains(index) else { throw BridgeError("Stale accessibility path") }
        element = list[index]
    }
    guard try text(element, kAXRoleAttribute) == role,
          try text(element, kAXDescriptionAttribute) == description else {
        throw BridgeError("Stale accessibility element: role or description changed")
    }
    if action == "typeText" {
        guard let value = request["text"] as? String, !value.isEmpty,
              let expectedCard = request["expected_card_description"] as? String, !expectedCard.isEmpty,
              value.unicodeScalars.allSatisfy({ !CharacterSet.controlCharacters.contains($0) && !CharacterSet.newlines.contains($0) }),
              ["AXTextArea", "AXTextField", "AXStaticText"].contains(role) || (role == "AXGenericElement" && description == "和Ta聊一聊~") else {
            throw BridgeError("typeText requires plain text, a chat identity and a known editor")
        }
        if request["expected_draft"] != nil && !(request["expected_draft"] is String) {
            throw BridgeError("Approved replacement draft must be an exact string")
        }
        func verifyChat() throws {
            var matches = 0
            var visited = 0
            func walk(_ node: AXUIElement, _ depth: Int) throws {
                visited += 1
                guard depth <= 40, visited < 10000 else { throw BridgeError("Chat identity tree exceeds supported size") }
                if try text(node, kAXDescriptionAttribute) == expectedCard,
                   try text(node, kAXRoleAttribute) == "AXButton" {
                    matches += 1
                }
                for child in try children(node) { try walk(child, depth + 1) }
            }
            try walk(window, 0)
            guard matches == 1 else { throw BridgeError("Current chat candidate changed; text was not submitted") }
        }
        try verifyChat()
        RunLoop.current.run(until: Date().addingTimeInterval(jitter(0.05)))
        let previousApplication = NSWorkspace.shared.frontmostApplication
        defer {
            // AXPress can ask UIKit to activate itself. Undo only that change;
            // this transport never activates BOSS or posts keyboard events.
            RunLoop.current.run(until: Date().addingTimeInterval(jitter(0.02)))
            if let previousApplication = previousApplication,
               previousApplication.processIdentifier != app.processIdentifier,
               NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier {
                _ = previousApplication.activate(options: [])
                RunLoop.current.run(until: Date().addingTimeInterval(jitter(0.05)))
            }
        }
        guard try actions(element).contains("AXPress"),
              AXUIElementPerformAction(element, kAXPressAction as CFString) == .success else {
            throw BridgeError("Cannot focus the chat editor")
        }
        var focus: AXUIElement?
        let focusDeadline = Date().addingTimeInterval(2)
        while Date() < focusDeadline {
            if let candidate = try attribute(application, kAXFocusedUIElementAttribute),
               CFGetTypeID(candidate) == AXUIElementGetTypeID() {
                let candidate = candidate as! AXUIElement
                let candidateRole = try text(candidate, kAXRoleAttribute)
                if ["AXStaticText", "AXTextArea", "AXTextField"].contains(candidateRole),
                   try attribute(candidate, kAXNumberOfCharactersAttribute) is NSNumber {
                    focus = candidate
                    break
                }
            }
            RunLoop.current.run(until: Date().addingTimeInterval(jitter(0.05)))
        }
        guard let focus = focus else { throw BridgeError("Focused editor does not expose readable text") }
        var names: CFArray?
        guard AXUIElementCopyParameterizedAttributeNames(focus, &names) == .success else {
            throw BridgeError("Cannot read focused editor text APIs")
        }
        let supported = names as? [String] ?? []
        guard supported.contains(kAXStringForRangeParameterizedAttribute),
              supported.contains("AXReplaceRangeWithText") else {
            throw BridgeError("Focused editor does not support readable AXReplaceRangeWithText")
        }
        let editorBounds = try rect(element)
        let focusBounds = try rect(focus)
        let focusFrame = CGRect(x: focusBounds["x"]!, y: focusBounds["y"]!,
                                width: focusBounds["width"]!, height: focusBounds["height"]!)
        let editorCenter = CGPoint(x: editorBounds["x"]! + editorBounds["width"]! / 2,
                                   y: editorBounds["y"]! + editorBounds["height"]! / 2)
        guard editorBounds["width"]! > 0, editorBounds["height"]! > 0,
              focusFrame.width > 0, focusFrame.height > 0, focusFrame.contains(editorCenter) else {
            throw BridgeError("Focused text element does not contain the intended chat editor")
        }
        func readText() throws -> String {
            guard let count = try attribute(focus, kAXNumberOfCharactersAttribute) as? NSNumber,
                  count.intValue >= 0 else { throw BridgeError("Cannot read editor character count") }
            // UIKit advertises the text API but rejects an empty range.
            if count.intValue == 0 { return "" }
            var range = CFRange(location: 0, length: count.intValue)
            guard let parameter = AXValueCreate(.cfRange, &range) else { throw BridgeError("Cannot create editor text range") }
            var content: CFTypeRef?
            let error = AXUIElementCopyParameterizedAttributeValue(focus, kAXStringForRangeParameterizedAttribute as CFString,
                                                                  parameter, &content)
            guard error == .success, let content = content as? String else {
                throw BridgeError("Cannot read complete editor text: AX error \(error.rawValue)")
            }
            return content
        }
        func verifyFocus() throws {
            guard let current = try attribute(application, kAXFocusedUIElementAttribute),
                  CFEqual(current, focus) else { throw BridgeError("Chat editor focus changed; text entry stopped") }
            try verifyChat()
        }
        try verifyFocus()
        let existing = try readText()
        if !existing.isEmpty {
            guard let approved = request["expected_draft"] as? String, existing == approved else {
                throw BridgeError("Chat editor draft does not match an approved replacement; it was not overwritten")
            }
        }
        var range = CFRange(location: 0, length: existing.utf16.count)
        guard let rangeValue = AXValueCreate(.cfRange, &range) else { throw BridgeError("Cannot encode the replacement range") }
        // AppKit's exported keys and AXValue(CFRange) encoding were verified
        // against an isolated owned NSTextView before use on this client.
        let payload: [String: Any] = ["AXReplacementRange": rangeValue, "AXReplacementText": value]
        try verifyFocus()
        guard try readText() == existing else { throw BridgeError("Editor draft changed before replacement; text preserved") }
        var response: CFTypeRef?
        let status = AXUIElementCopyParameterizedAttributeValue(focus, "AXReplaceRangeWithText" as CFString,
                                                               payload as CFDictionary, &response)
        guard status == .success else { throw BridgeError("AXReplaceRangeWithText failed: AX error \(status.rawValue)") }
        if let response = response, CFGetTypeID(response) == CFBooleanGetTypeID(),
           !CFBooleanGetValue((response as! CFBoolean)) {
            throw BridgeError("AXReplaceRangeWithText rejected the replacement; no retry")
        }
        let readDeadline = Date().addingTimeInterval(1.5)
        var verified = try readText()
        while verified != value && Date() < readDeadline {
            RunLoop.current.run(until: Date().addingTimeInterval(jitter(0.05)))
            try verifyFocus()
            verified = try readText()
        }
        try verifyFocus()
        guard verified == value else { throw BridgeError("Complete replacement text could not be verified; draft preserved, no retry") }
        return ["ok": true, "text": verified]
    }
    if action == "setValue" {
        guard ["AXTextArea", "AXTextField"].contains(role), let value = request["text"] as? String else {
            throw BridgeError("setValue requires a text field and text")
        }
        var settable = DarwinBoolean(false)
        let check = AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable)
        guard check == .success, settable.boolValue else { throw BridgeError("Text field value is not settable") }
        let result = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFString)
        guard result == .success else { throw BridgeError("setValue failed: AX error \(result.rawValue)") }
        return ["ok": true]
    }
    guard try actions(element).contains(action) else { throw BridgeError("Element does not support \(action)") }
    let error = AXUIElementPerformAction(element, action as CFString)
    guard error == .success else { throw BridgeError("Action \(action) failed: AX error \(error.rawValue)") }
    return ["ok": true]
}

do {
    let result = try run()
    let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
} catch {
    let data = try! JSONSerialization.data(withJSONObject: ["error": String(describing: error)])
    FileHandle.standardError.write(data)
    FileHandle.standardError.write(Data([10]))
    exit(1)
}
